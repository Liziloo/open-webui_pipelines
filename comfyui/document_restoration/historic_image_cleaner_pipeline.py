import os
import time
import json
import base64
import re
import requests
import io
import hashlib
from PIL import Image
from typing import List, Union, Generator, Iterator, Any, Optional
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
        TEXT_MODEL: str = "llama3.1:8b" # Faster text-only model for the loop
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

    def generate_triage_artifacts(self, body: OpenWebUIBody, base64_image: str):
        page_id = self.generate_page_id(base64_image)
        project_id = body.get("chat_id", "chat_experimental")
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
        """PASS 0: The 'Single Glance' using the Vision Model."""
        with open(thumb_path, "rb") as f:
            thumb_b64 = base64.b64encode(f.read()).decode('utf-8')

        prompt = (
            "Analyze this archival document thumbnail. Identify geometry, noise, and thresholding needs. "
            "Reply ONLY with a raw JSON object: "
            "{\"geometry\": {\"active\": bool, \"rotation_angle\": int}, "
            "\"denoise\": {\"active\": bool, \"sigma_color\": float}, "
            "\"threshold\": {\"active\": bool, \"level\": float}}"
        )
        
        payload = {
            "model": self.valves.VISION_MODEL,
            "messages": [{"role": "user", "content": prompt, "images": [thumb_b64]}],
            "format": "json", "stream": False
        }
        
        res = requests.post(f"{self.valves.OLLAMA_URL}/api/chat", json=payload).json()
        return json.loads(res["message"]["content"])

    def get_next_adjustment(self, state: dict, current_ocr_confidence: float) -> dict:
        """PASS 1+: Text-only feedback loop. No images sent."""
        prompt = (
            f"Initial Triage Strategy: {json.dumps(state['policy_notes'])}\n"
            f"Previous Attempts: {json.dumps(state['history'])}\n"
            f"Current OCR Confidence: {current_ocr_confidence}\n"
            "Based on the OCR score, adjust the parameters to improve readability. "
            "Return ONLY the updated JSON parameters."
        )
        
        payload = {
            "model": self.valves.TEXT_MODEL,
            "messages": [{"role": "user", "content": prompt}],
            "format": "json", "stream": False
        }
        
        res = requests.post(f"{self.valves.OLLAMA_URL}/api/chat", json=payload).json()
        return json.loads(res["message"]["content"])

    def process_in_comfyui(self, image_bytes: bytes, report: dict) -> bytes:
        files = {"image": ("working.png", image_bytes, "image/png")}
        upload_res = requests.post(f"{self.valves.COMFYUI_URL}/upload/image", files=files).json()
        
        with open(self.valves.WORKFLOW_JSON_PATH, "r") as f:
            workflow = json.load(f)

        workflow["1"]["inputs"]["image"] = upload_res["name"]
        
        # Mapping (Simplified for brevity)
        workflow["27"]["inputs"]["value"] = report.get("denoise", {}).get("active", False)
        workflow["20"]["inputs"]["value"] = report.get("geometry", {}).get("active", False)
        workflow["22"]["inputs"]["value"] = report.get("threshold", {}).get("active", False)
        
        if report.get("threshold", {}).get("active"):
            workflow["2"]["inputs"]["threshold"] = report["threshold"]["level"]

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
        # 1. Image Extraction (Omitted for brevity, same as before)
        base64_image = self._extract_image(messages) 
        if not base64_image: yield "No image found."; return

        # 2. Pass 0: The Single Glance
        yield "👁️ **Pass 0: Initial Vision Triage...**\n"
        page_id, hdd_path, thumb_path = self.generate_triage_artifacts(body, base64_image)
        initial_policy = self.get_initial_triage(thumb_path)
        
        state = self.update_page_state(page_id, {
            "page_metadata": {"page_id": page_id, "source_path": hdd_path},
            "policy_notes": initial_policy,
            "history": []
        })

        current_bytes = base64.b64decode(base64_image)
        current_confidence = 0.0 # Placeholder for OCR
        iteration = 0

        # 3. Iterative Text-Only Loop
        while iteration < self.valves.MAX_ITERATIONS and current_confidence < 0.9:
            iteration += 1
            yield f"\n### Pass {iteration}\n"
            
            # Determine parameters: Use initial policy for Pass 1, then Text-LLM for tweaks
            if iteration == 1:
                params = initial_policy
            else:
                yield "- 🧠 Text-LLM calculating adjustments based on OCR..."
                params = self.get_next_adjustment(state, current_confidence)
            
            yield f"\n- **Parameters**: `{json.dumps(params)}`"
            
            # Execute and Render
            current_bytes = self.process_in_comfyui(current_bytes, params)
            
            # MOCK OCR STEP (We'll wire this to Kraken/Doctr next)
            current_confidence += 0.3 
            
            state["history"].append({"pass": iteration, "params": params, "conf": current_confidence})
            self.update_page_state(page_id, {"history": state["history"]})

            # Render logic (Omitted for brevity)
            yield f"\n- ✅ Pass {iteration} complete. Confidence: {current_confidence:.2f}"

        yield f"\n\n🎉 **Done.** Archive: `{page_id}`"

    def _extract_image(self, messages):
        for msg in reversed(messages):
            if isinstance(msg.get("content"), list):
                for item in msg["content"]:
                    if item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        return url.split(",")[1] if "," in url else url
        return None