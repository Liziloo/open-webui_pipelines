import os
import time
import json
import base64
import requests
import io
import hashlib
from PIL import Image
from typing import List, Union, Generator
from pydantic import BaseModel
from typing import TypedDict

class OpenWebUIBody(TypedDict, total=False):
    chat_id: str
    model: str
    session_id: str
    metadata: dict

class Pipeline:
    class Valves(BaseModel):
        OLLAMA_URL: str = "http://192.168.2.86:11434"
        COMFYUI_URL: str = "http://192.168.2.86:8188"
        VISION_MODEL: str = "llama3.2-vision:latest"
        TEXT_MODEL: str = "llama3.1:8b"
        MAX_ITERATIONS: int = 3
        WORKFLOW_JSON_PATH: str = "/app/backend/data/document_restoration_flow.json"
        BASE_DATA_DIR: str = "/mnt/data/projects/open_webui_chats"
        SESSION_DIR: str = "/app/pipelines/historic_image_cleaner/active_sessions"
        TEMP_DIR: str = "/app/pipelines/historic_image_cleaner/temp"

    def __init__(self):
        self.valves = self.Valves()

    def generate_page_id(self, base64_image: str) -> str:
        img_bytes = base64.b64decode(base64_image)
        return hashlib.sha256(img_bytes).hexdigest()[:8]

    def generate_triage_artifacts(self, project_id: str, base64_image: str):
        page_id = self.generate_page_id(base64_image)
        hdd_base = os.path.join(self.valves.BASE_DATA_DIR, project_id, page_id)
        os.makedirs(hdd_base, exist_ok=True)
        os.makedirs(self.valves.TEMP_DIR, exist_ok=True)

        hdd_path = os.path.join(hdd_base, "original.tif")
        ssd_thumb_path = os.path.join(self.valves.TEMP_DIR, f"{page_id}_thumb.jpg")

        img_data = base64.b64decode(base64_image)
        with open(hdd_path, "wb") as f:
            f.write(img_data)

        with Image.open(io.BytesIO(img_data)) as img:
            img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "P"): img = img.convert("RGB")
            img.save(ssd_thumb_path, "JPEG", quality=85)
        
        return page_id, hdd_path, ssd_thumb_path

    def update_page_state(self, page_id: str, updates: dict) -> dict:
        state_path = os.path.join(self.valves.SESSION_DIR, f"{page_id}_page_state.json")
        os.makedirs(self.valves.SESSION_DIR, exist_ok=True)
        state = {}
        if os.path.exists(state_path):
            with open(state_path, "r") as f:
                state = json.load(f)
        state.update(updates)
        with open(state_path, "w") as f:
            json.dump(state, f, indent=2)
        return state

    def get_initial_triage(self, thumb_path: str) -> dict:
        with open(thumb_path, "rb") as f:
            thumb_b64 = base64.b64encode(f.read()).decode('utf-8')
        prompt = (
            "Analyze this archival document thumbnail. Identify geometry, noise, and thresholding needs. "
            "Reply ONLY with a raw JSON object: "
            "{\"geometry\": {\"active\": bool, \"rotation_angle\": int}, "
            "\"denoise\": {\"active\": bool, \"sigma_color\": float, \"sigma_space\": float}, "
            "\"threshold\": {\"active\": bool, \"level\": float}}"
        )
        payload = {"model": self.valves.VISION_MODEL, "messages": [{"role": "user", "content": prompt, "images": [thumb_b64]}], "format": "json", "stream": False}
        res = requests.post(f"{self.valves.OLLAMA_URL}/api/chat", json=payload).json()
        return json.loads(res["message"]["content"])

    def get_next_adjustment(self, state: dict, current_ocr_confidence: float) -> dict:
        prompt = (
            f"Initial Triage Strategy: {json.dumps(state['policy_notes'])}\n"
            f"Previous Attempts: {json.dumps(state['history'])}\n"
            f"Current OCR Confidence: {current_ocr_confidence}\n"
            "Adjust the parameters to improve readability. Return ONLY the updated JSON parameters."
        )
        payload = {"model": self.valves.TEXT_MODEL, "messages": [{"role": "user", "content": prompt}], "format": "json", "stream": False}
        res = requests.post(f"{self.valves.OLLAMA_URL}/api/chat", json=payload).json()
        return json.loads(res["message"]["content"])

    def process_in_comfyui(self, project_id: str, page_id: str, iteration: int, image_bytes: bytes, report: dict) -> bytes:
        files = {"image": (f"{page_id}_pass{iteration}.png", image_bytes, "image/png")}
        upload_res = requests.post(f"{self.valves.COMFYUI_URL}/upload/image", files=files).json()
        
        with open(self.valves.WORKFLOW_JSON_PATH, "r") as f:
            workflow = json.load(f)

        workflow["1"]["inputs"]["image"] = upload_res["name"]
        
        # --- Mapping Logic ---
        denoise = report.get("denoise", {})
        workflow["27"]["inputs"]["value"] = denoise.get("active", False)
        if denoise.get("active"):
            workflow["26"]["inputs"]["sigma_color"] = denoise.get("sigma_color", 10.0)
            workflow["26"]["inputs"]["sigma_space"] = denoise.get("sigma_space", 10.0)

        geo = report.get("geometry", {})
        workflow["20"]["inputs"]["value"] = geo.get("active", False)
        if geo.get("active"):
            workflow["6"]["inputs"]["rotation"] = geo.get("rotation_angle", 0)

        thresh = report.get("threshold", {})
        workflow["22"]["inputs"]["value"] = thresh.get("active", False)
        if thresh.get("active"):
            workflow["2"]["inputs"]["threshold"] = thresh.get("level", 0.20)

        # Write workflow to HDD audit trail
        pass_workflow_path = os.path.join(self.valves.BASE_DATA_DIR, project_id, page_id, f"pass_{iteration}_workflow.json")
        with open(pass_workflow_path, "w") as f:
            json.dump(workflow, f, indent=2)

        prompt_res = requests.post(f"{self.valves.COMFYUI_URL}/prompt", json={"prompt": workflow}).json()
        prompt_id = prompt_res["prompt_id"]

        for _ in range(60):
            hist = requests.get(f"{self.valves.COMFYUI_URL}/history/{prompt_id}").json()
            if prompt_id in hist:
                out = hist[prompt_id]["outputs"]["23"]["images"][0]["filename"]
                return requests.get(f"{self.valves.COMFYUI_URL}/view?filename={out}").content
            time.sleep(1.5)
        raise TimeoutError("ComfyUI Timeout")

    def pipe(self, user_message: str, model_id: str, messages: List[dict], body: OpenWebUIBody) -> Union[str, Generator]:
        project_id = body.get("chat_id", "manual_upload")
        base64_image = self._extract_image(messages) 
        if not base64_image: yield "No image found."; return

        yield "👁️ **Pass 0: Initial Vision Triage...**\n"
        page_id, hdd_path, thumb_path = self.generate_triage_artifacts(project_id, base64_image)
        initial_policy = self.get_initial_triage(thumb_path)
        
        state = self.update_page_state(page_id, {
            "page_metadata": {"page_id": page_id, "project_id": project_id, "source_path": hdd_path},
            "policy_notes": initial_policy,
            "history": []
        })

        current_bytes = base64.b64decode(base64_image)
        current_confidence = 0.0
        iteration = 0

        while iteration < self.valves.MAX_ITERATIONS and current_confidence < 0.9:
            iteration += 1
            yield f"\n### Pass {iteration}\n"
            params = initial_policy if iteration == 1 else self.get_next_adjustment(state, current_confidence)
            yield f"- **Parameters**: `{json.dumps(params)}`"
            
            try:
                current_bytes = self.process_in_comfyui(project_id, page_id, iteration, current_bytes, params)
                current_confidence += 0.3 # Mock OCR increment
                state["history"].append({"pass": iteration, "params": params, "conf": current_confidence})
                self.update_page_state(page_id, {"history": state["history"]})
                
                img_b64 = base64.b64encode(current_bytes).decode('utf-8')
                md_img = f"\n- ✅ Pass {iteration} complete (Confidence: {current_confidence:.2f})\n\n![Pass {iteration}](data:image/jpeg;base64,{img_b64})\n"
                for i in range(0, len(md_img), 64*1024):
                    yield md_img[i:i+(64*1024)]
            except Exception as e:
                yield f"\n❌ **Error:** {e}"; return

        yield f"\n\n🎉 **Done.** Archive: `{page_id}`"

    def _extract_image(self, messages):
        for msg in reversed(messages):
            if isinstance(msg.get("content"), list):
                for item in msg["content"]:
                    if item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        return url.split(",")[1] if "," in url else url
        return None