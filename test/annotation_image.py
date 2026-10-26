"""
Load the annotations of the shot's task whose status is WFA
(Waiting For Approval) from Kitsu and store them in test_image.json.

Requires: pip install gazu
"""

import json
import sys

import gazu

KITSU_API = "http://localhost/api"
EMAIL = "basile@cg-wire.com"
PASSWORD = "mysecretpassword"

# From: /productions/<project_id>/episodes/<episode_id>/shots/<shot_id>
SHOT_ID = "9077f083-ed4b-46c2-9c9a-8130d927bbc4"
TASK_STATUS_SHORT_NAME = "wfa"
OUTPUT_FILE = "test_image.json"


def get_task_status(short_name):
    """Return the task status matching short_name (case-insensitive)."""
    for status in gazu.task.all_task_statuses():
        if status["short_name"].lower() == short_name.lower():
            return status
    return None


def main():
    # 1. Log in
    gazu.set_host(KITSU_API)
    gazu.log_in(EMAIL, PASSWORD)

    # 2. Find the shot's task whose status is WFA
    shot = gazu.shot.get_shot(SHOT_ID)
    wfa_status = get_task_status(TASK_STATUS_SHORT_NAME)
    if wfa_status is None:
        sys.exit(f"Task status '{TASK_STATUS_SHORT_NAME}' not found.")

    wfa_tasks = [
        t
        for t in gazu.task.all_tasks_for_shot(shot)
        if t["task_status_id"] == wfa_status["id"]
    ]
    if not wfa_tasks:
        sys.exit(f"No task with status {wfa_status['name']} for shot {shot['name']}.")

    if len(wfa_tasks) > 1:
        # Several tasks waiting for approval: take the most recently updated one.
        wfa_tasks.sort(key=lambda t: t.get("updated_at") or "", reverse=True)
        print(
            f"Warning: {len(wfa_tasks)} WFA tasks found for shot {shot['name']}, "
            "using the most recently updated one."
        )
    task = wfa_tasks[0]
    task_type = gazu.task.get_task_type(task["task_type_id"])

    # 3. Collect annotations from every preview attached to the task's comments
    previews = []
    for comment in gazu.task.all_comments_for_task(task):
        for preview in comment.get("previews", []):
            preview_id = preview["id"] if isinstance(preview, dict) else preview
            preview_file = gazu.files.get_preview_file(preview_id)
            annotations = preview_file.get("annotations") or []
            if not annotations:
                continue
            previews.append(
                {
                    "preview_file_id": preview_id,
                    "revision": preview_file.get("revision"),
                    "extension": preview_file.get("extension"),
                    "comment_id": comment["id"],
                    "annotations": annotations,
                }
            )

    previews.sort(key=lambda p: p["revision"] or 0)

    # 4. Store in JSON
    data = {
        "shot_id": shot["id"],
        "shot_name": shot["name"],
        "task_id": task["id"],
        "task_type": task_type["name"],
        "task_status": wfa_status["name"],
        "previews": previews,
    }
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

    print(f"Saved annotations from {len(previews)} preview(s) to {OUTPUT_FILE}")
    gazu.log_out()


if __name__ == "__main__":
    main()