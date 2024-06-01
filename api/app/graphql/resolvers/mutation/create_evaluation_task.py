import asyncio
import os
import re
import time

from asgiref.sync import sync_to_async
from strawberry import ID
from strawberry.types import Info

from libs.models import EvaluationTask, GenerationTask, Rate
from libs.models.evaluation_task import Status as EvaluationTaskStatus
from libs.models.generation_task import Status as GenerationTaskStatus
from libs.services.gen_answer import chat_with_job_info


def extract_and_convert_to_int(input_str):
    match = re.search(r"\d+", input_str)
    if match:
        return int(match.group())
    else:
        return 0


async def resolve(info: Info, generation_task_id: ID, eval_name: str, model: str, worker_count: int):
    api_key = "EMPTY"
    if model.startswith("gpt"):
        api_key = os.getenv("OPENAI_API_KEY")
    if model.startswith("gemini"):
        api_key = os.getenv("GEMINI_API_KEY")
    if model.startswith("claude"):
        api_key = os.getenv("ANTHROPIC_API_KEY")
    if model.startswith("command"):
        api_key = os.getenv("COHERE_API_KEY")

    user = info.context.user
    generation_task = await GenerationTask.objects.select_related("bench").aget(
        id=generation_task_id, user=user, status=GenerationTaskStatus.COMPLETED
    )
    evaluation_task = await EvaluationTask.objects.acreate(
        user=user, generation_task=generation_task, name=eval_name, points={}, processing_times={}, status=EvaluationTaskStatus.STARTED
    )

    template = generation_task.bench.template

    try:
        jobs = []
        async for answer in generation_task.answers.select_related("question").order_by("id").all():
            question = answer.messages[0]["content"]
            correct_answer = answer.question.correct_answers[0] if answer.question.correct_answers else None
            eval_aspect = answer.question.eval_aspects[0] if answer.question.eval_aspects else None
            content = template.format(question=question, answer=answer.text, correct_answer=correct_answer, eval_aspect=eval_aspect)
            messages = [
                {"role": "system", "content": "評価の点数は必ず[[数字]]の形式で示す。説明は簡潔にする。"},
                {"role": "user", "content": content},
            ]
            # if correct_answers:
            #     messages.append({"role": "user", "content": f"正しい答えは次のようになります。{correct_answers[0]}"})
            params = {"temperature": 0, "max_tokens": 1500}
            jobs.append(chat_with_job_info(answer, messages, model, host=None, api_key=api_key, strategy="none", params=params))
            if len(jobs) == worker_count:
                results = await asyncio.gather(*(asyncio.wait_for(job, timeout=180) for job in jobs), return_exceptions=True)
                jobs = []
                for result in results:
                    try:
                        if isinstance(result, asyncio.exceptions.CancelledError):
                            raise Exception("Timeout")
                        answer = result["response"]["answer"]
                        match = re.search(r"\[\[(.+)\]\]", answer)
                        point = 0
                        if match is not None:
                            point = extract_and_convert_to_int(match.group(1))
                        if point == 0 and answer.isdigit():
                            point = int(answer)
                        await Rate.objects.acreate(
                            user=user,
                            evaluation_task=evaluation_task,
                            answer=result["info"],
                            text=result["response"]["answer"],
                            usage=result["response"]["usage"],
                            finish_reason=result["response"]["finish_reason"],
                            processing_time=result["processing_time"],
                            point=point,
                            model=model,
                        )
                    except Exception as e:
                        print(result)
                        raise e
                if model.startswith("gemini"):
                    time.sleep(10)
                if model.startswith("gemini/gemini-1.5"):
                    time.sleep(15)
                if model.startswith("claude"):
                    time.sleep(10)
                if model.startswith("command"):
                    time.sleep(5)
        evaluation_task.status = EvaluationTaskStatus.COMPLETED
        await sync_to_async(lambda: evaluation_task.save())()
        return evaluation_task
    except Exception as e:
        print(e)
        evaluation_task.status = EvaluationTaskStatus.FAILED
        await sync_to_async(lambda: evaluation_task.save())()
        return evaluation_task
