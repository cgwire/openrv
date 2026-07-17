#!/usr/bin/env python3
# python3 src/kitsu_import_annotations.py

import gazu
import json

gazu.set_host("http://localhost:80/api")
gazu.log_in("admin@example.com", "mysecretpassword")

def push_to_kitsu(
    preview_file,
    additions,
    updates,
    deletions,
):
    return gazu.files.update_preview_annotations(
        preview_file,
        additions=additions,
        updates=updates,
        deletions=deletions,
    )

with open('additions.json', 'r') as file:
    additions = json.load(file)
    push_to_kitsu("5e0ecd69-1559-41a3-b4da-dc1c9d1e0b5c", additions, [], [])
    print("ok")