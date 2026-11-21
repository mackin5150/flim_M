"""OpenAI screenplay planning and a local ComfyUI workflow adapter.

The WAN workflow must be exported in ComfyUI API format and configured explicitly.
No paid video API is used.
"""
import copy
import json
import os
import time
from pathlib import Path

import httpx

class OpenAIPlanner:
    def __init__(self):
        self.key = os.getenv("OPENAI_API_KEY", "")
        self.model = os.getenv("OPENAI_PLANNING_MODEL", "gpt-4.1-mini")
        if not self.key:
            raise ValueError("Set OPENAI_API_KEY for AI planning")

    def _json(self, system, payload):
        with httpx.Client(timeout=120) as client:
            response = client.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + self.key},
                json={"model": self.model, "response_format": {"type": "json_object"},
                      "messages": [{"role": "system", "content": system + " Return a JSON object only."},
                                   {"role": "user", "content": json.dumps(payload)}]})
        if not response.is_success:
            raise ValueError("OpenAI planning HTTP " + str(response.status_code) + "; check credits, access and model")
        return json.loads(response.json()["choices"][0]["message"]["content"])

    def plan(self, project):
        result = self._json(
            'Convert the screenplay into <=30 scenes. Output {"scenes":[{"title":"...","direction":"...","duration":12,"kind":"Establishing","cast":["character-id"]}]}. '
            'Each duration 4-60 seconds; kind Dialogue, Establishing, Chase or Revelation. '
            'Use only the supplied character IDs, preserve exact dialogue, costumes and continuity.',
            project.model_dump())
        return result["scenes"]

    def shot_directions(self, direction, durations):
        result = self._json(
            'Break a film scene into sequential shots. Output {"shots":["..."]} with exactly one detailed shot direction per duration. '
            'Keep the same character identity, wardrobe, camera geography and exact dialogue.',
            {"direction": direction, "durations": durations})
        shots = result["shots"]
        if len(shots) != len(durations) or not all(isinstance(s, str) and s.strip() for s in shots):
            raise ValueError("OpenAI returned an invalid shot breakdown")
        return shots


class ComfyWAN:
    def __init__(self):
        self.url = os.getenv("COMFYUI_URL", "http://127.0.0.1:8188").rstrip("/")
        self.path = os.getenv("COMFYUI_WORKFLOW", "")
        self.prompt_node = os.getenv("COMFYUI_PROMPT_NODE", "")
        self.image_node = os.getenv("COMFYUI_IMAGE_NODE", "")
        if not self.path or not self.prompt_node:
            raise ValueError("Configure COMFYUI_WORKFLOW and COMFYUI_PROMPT_NODE (API-format workflow)")
        self.template = json.loads(Path(self.path).read_text())
        if not isinstance(self.template, dict) or self.prompt_node not in self.template:
            raise ValueError("Invalid ComfyUI API workflow or prompt node ID")

    def submit(self, prompt, references, duration):
        workflow = copy.deepcopy(self.template)
        node = workflow[self.prompt_node]
        if "text" not in node.get("inputs", {}):
            raise ValueError("Prompt node must have an inputs.text field")
        node["inputs"]["text"] = prompt
        if references:
            if not self.image_node or self.image_node not in workflow:
                raise ValueError("Set COMFYUI_IMAGE_NODE to the LoadImage node to use character references")
            # ComfyUI LoadImage accepts one start image. Remaining character sheets
            # are described in the prompt; no unsupported multi-reference claim.
            with httpx.Client(timeout=120) as client:
                with open(references[0], "rb") as f:
                    upload = client.post(self.url + "/upload/image", files={"image": (references[0].name, f, "image/jpeg")}, data={"overwrite": "false"})
            upload.raise_for_status()
            workflow[self.image_node]["inputs"]["image"] = upload.json()["name"]
        with httpx.Client(timeout=60) as client:
            response = client.post(self.url + "/prompt", json={"prompt": workflow})
        response.raise_for_status()
        data = response.json()
        if data.get("error"):
            raise ValueError("ComfyUI rejected workflow: " + str(data["error"])[:300])
        return data["prompt_id"]

    def poll(self, task_id):
        with httpx.Client(timeout=30) as client:
            response = client.get(self.url + "/history/" + task_id)
        response.raise_for_status()
        item = response.json().get(task_id)
        if not item:
            return {"status": "running"}
        if item.get("status", {}).get("status_str") == "error":
            return {"status": "failed"}
        for output in item.get("outputs", {}).values():
            for key in ("gifs", "videos"):
                for video in output.get(key, []):
                    if video.get("filename", "").lower().endswith(".mp4"):
                        return {"status": "succeeded", "video": video}
        raise ValueError("ComfyUI completed without an MP4 video output; configure VideoCombine MP4 output")

    def download(self, video, destination):
        with httpx.Client(timeout=180) as client:
            with client.stream("GET", self.url + "/view",
                params={"filename": video["filename"], "subfolder": video.get("subfolder", ""),
                        "type": video.get("type", "output")}) as response:
                response.raise_for_status()
                with open(destination, "wb") as f:
                    for chunk in response.iter_bytes():
                        f.write(chunk)
