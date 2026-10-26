"""
Load the annotations of the WFA (Waiting For Approval) task(s) of a shot
from Kitsu and store them in test_video.json.

Requires: pip install gazu
"""
import json

import gazu

HOST = "http://localhost/api"
EMAIL = "basile@cg-wire.com"
PASSWORD = "mysecretpassword"

PROJECT_ID = "5b388012-7bda-4b97-a7be-9702e10a2968"
EPISODE_ID = "640f4073-f138-47ac-bed9-43106fd90510"
SHOT_ID = "32f11d18-7ad3-4aab-b58a-3a6687c27600"

OUTPUT_FILE = "test_video.json"


def get_wfa_status():
    """Return the task status whose short name is WFA (case-insensitive)."""
    for status in gazu.task.all_task_statuses():
        if status["short_name"].lower() == "wfa":
            return status
    raise SystemExit("No task status with short name 'wfa' found.")


def get_task_annotations(task):
    """Collect annotations from every preview attached to the task's comments."""
    previews = []
    for comment in gazu.task.all_comments_for_task(task):
        for preview in comment.get("previews", []):
            preview_id = preview["id"] if isinstance(preview, dict) else preview
            preview_file = gazu.files.get_preview_file(preview_id)
            previews.append(
                {
                    "preview_file_id": preview_file["id"],
                    "comment_id": comment["id"],
                    "revision": preview_file.get("revision"),
                    "extension": preview_file.get("extension"),
                    "created_at": preview_file.get("created_at"),
                    "annotations": preview_file.get("annotations") or [],
                }
            )
    previews.sort(key=lambda p: p["revision"] or 0)
    return previews


def main():
    gazu.set_host(HOST)
    gazu.log_in(EMAIL, PASSWORD)

    try:
        shot = gazu.shot.get_shot(SHOT_ID)
        wfa_status = get_wfa_status()

        wfa_tasks = [
            task
            for task in gazu.task.all_tasks_for_shot(shot)
            if task["task_status_id"] == wfa_status["id"]
        ]
        if not wfa_tasks:
            print(f"No WFA task found for shot {shot['name']}.")

        result = {
            "project_id": PROJECT_ID,
            "episode_id": EPISODE_ID,
            "shot": {"id": shot["id"], "name": shot["name"]},
            "tasks": [],
        }

        for task in wfa_tasks:
            task_type = gazu.task.get_task_type(task["task_type_id"])
            previews = get_task_annotations(task)
            result["tasks"].append(
                {
                    "task_id": task["id"],
                    "task_type": task_type["name"],
                    "task_status": wfa_status["short_name"],
                    "previews": previews,
                }
            )
            count = sum(len(p["annotations"]) for p in previews)
            print(
                f"{task_type['name']}: {len(previews)} preview(s), "
                f"{count} annotation frame(s)"
            )

        with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
            json.dump(result, f, indent=2, ensure_ascii=False)
        print(f"Annotations saved to {OUTPUT_FILE}")

    finally:
        gazu.log_out()


if __name__ == "__main__":
    main()