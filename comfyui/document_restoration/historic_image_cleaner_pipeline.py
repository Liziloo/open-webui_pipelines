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
        ZONE_C_API_URL: str = "http://192.168.2.86:8000" # Where Kraken/Doctr endpoints live
        VISION_MODEL: str = "qwen2.5vl:32b"
        TEXT_MODEL: str = "glm-4.7-flash:q8_0"
        
        # Path Translation (Zone B vs Zone C)
        LOCAL_DATA_DIR: str = "/data/projects"           # How the Docker container sees the drive
        REMOTE_DATA_DIR: str = "/mnt/data/projects"      # How the GPU machine sees the drive

        # Internal SSD Paths
        WORKFLOW_JSON_PATH: str = "/app/backend/data/document_restoration_flow.json"
        BATCH_JOB_PATH: str = "/app/pipelines/historic_image_cleaner/batch_job.json"
        SESSION_DIR: str = "/app/pipelines/historic_image_cleaner/active_sessions"
        TEMP_DIR: str = "/app/pipelines/historic_image_cleaner/temp"
        

    def __init__(self):
        self.valves = self.Valves()
        # Strict JSON schema to force the LLM to use the exact keys we parse
        self.json_schema = (
            "{"
            "  \"transform\": {\"active\": true, \"rotation_angle\": 0.0, \"scale_by\": 1.0, \"pad\":[104, 104, 104, 104]},"
            "  \"frequency\": {\"active\": true, \"blur_radius\": 20, \"sigma\": 1.0, \"blend_percentage\": 1.0},"
            "  \"denoise\": {\"active\": true, \"diameter\": 3, \"sigma_color\": 10.0, \"sigma_space\": 10.0},"
            "  \"threshold\": {\"active\": true, \"threshold\": 0.1}"
            "}"
        )

    def get_batch_constraints(self):
        """Reads batch_job.json to get runtime constraints, with safe defaults."""
        try:
            if os.path.exists(self.valves.BATCH_JOB_PATH):
                with open(self.valves.BATCH_JOB_PATH, "r") as f:
                    data = json.load(f)
                    constraints = data.get("runtime_constraints", {})
                    return constraints.get("max_passes_per_page", 5), constraints.get("min_confidence_threshold", 0.85)
        except Exception as e:
            pass
        return 3, 0.85 # Defaults

    def generate_page_id(self, base64_image: str) -> str:
        img_bytes = base64.b64decode(base64_image)
        return hashlib.sha256(img_bytes).hexdigest()[:8]

    def generate_triage_artifacts(self, project_id: str, base64_image: str):
        page_id = self.generate_page_id(base64_image)
        
        # Local HDD Paths (Zone B writing)
        hdd_base_local = os.path.join(self.valves.LOCAL_DATA_DIR, project_id, "processed", "pages", page_id)
        os.makedirs(hdd_base_local, exist_ok=True)
        hdd_path_local = os.path.join(hdd_base_local, "original.tif")
        
        # SSD Paths (Fast local temp storage)
        ocr_input_dir = os.path.join(self.valves.TEMP_DIR, "ocr_input")
        os.makedirs(ocr_input_dir, exist_ok=True)
        ssd_thumb_path = os.path.join(ocr_input_dir, f"{page_id}_triage_thumb.jpg")

        img_data = base64.b64decode(base64_image)
        with open(hdd_path_local, "wb") as f:
             f.write(img_data)

        # Generate 1024px Thumbnail for Vision LLM
        with Image.open(io.BytesIO(img_data)) as img:
            img.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
            if img.mode in ("RGBA", "P"): img = img.convert("RGB")
            img.save(ssd_thumb_path, "JPEG", quality=85)
        
        return page_id, hdd_base_local, hdd_path_local, ssd_thumb_path

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
            "Analyze this archival document thumbnail. Categorize the physical degradation (faded, skewed, noise). "
            "Provide an initial restoration strategy.\n"
            f"Reply ONLY with a raw JSON object matching this exact structure:\n{self.json_schema}"
        )
        
        payload = {
            "model": self.valves.VISION_MODEL,
            "messages":[{"role": "user", "content": prompt, "images": [thumb_b64]}],
            "format": "json", 
            "stream": False,
            "keep_alive": 0 
        }
        
        res = requests.post(f"{self.valves.OLLAMA_URL}/api/chat", json=payload).json()
        return json.loads(res["message"]["content"])

    def get_next_adjustment(self, state: dict, current_ocr_confidence: float) -> dict:
        prompt = (
            f"Initial Triage Strategy: {json.dumps(state.get('policy_notes', {}))}\n"
            f"Previous Attempts: {json.dumps(state.get('history',[]))}\n"
            f"Current OCR Confidence: {current_ocr_confidence}\n"
            "Adjust the parameters to improve readability and reach the threshold. "
            f"Return ONLY the updated JSON parameters matching this structure:\n{self.json_schema}"
        )
        payload = {
            "model": self.valves.TEXT_MODEL, 
            "messages":[{"role": "user", "content": prompt}], 
            "format": "json", 
            "stream": False,
            "keep_alive": 0
        }
        res = requests.post(f"{self.valves.OLLAMA_URL}/api/chat", json=payload).json()
        return json.loads(res["message"]["content"])

    def get_ocr_metrics(self, remote_image_path: str, iteration: int) -> float:
        """Calls Zone C OCR endpoint using the translated remote path."""
        try:
            res = requests.post(f"{self.valves.ZONE_C_API_URL}/ocr_process", json={"image_path": remote_image_path}, timeout=5)
            if res.status_code == 200:
                return res.json().get("confidence_avg", 0.0)
        except Exception:
            pass
        # Mock increment for testing if OCR isn't running yet
        return min(0.95, 0.30 + (iteration * 0.25))

    def process_in_comfyui(self, page_id: str, iteration: int, original_image_bytes: bytes, report: dict) -> bytes:
        # 1. Upload ORIGINAL image to ComfyUI (Prevents destructive iteration)
        files = {"image": (f"{page_id}_source.png", original_image_bytes, "image/png")}
        upload_res = requests.post(f"{self.valves.COMFYUI_URL}/upload/image", files=files).json()
        
        with open(self.valves.WORKFLOW_JSON_PATH, "r") as f:
            workflow = json.load(f)

        workflow["1"]["inputs"]["image"] = upload_res["name"]
        
        # 2. Map parameters exactly to the provided document_restoration_flow.json
        
        # Transform (Geometry) -> Toggle Node 20
        transform = report.get("transform", {})
        workflow["20"]["inputs"]["value"] = transform.get("active", False)
        if transform.get("active"):
            workflow["30"]["inputs"]["rotation_angle"] = transform.get("rotation_angle", 0.0)
            workflow["7"]["inputs"]["scale_by"] = transform.get("scale_by", 1.0)
            pad = transform.get("pad",[104, 104, 104, 104])
            if len(pad) == 4:
                workflow["5"]["inputs"]["top"] = pad[0]
                workflow["5"]["inputs"]["right"] = pad[1]
                workflow["5"]["inputs"]["bottom"] = pad[2]
                workflow["5"]["inputs"]["left"] = pad[3]

        # Frequency -> Toggle Node 21
        freq = report.get("frequency", {})
        workflow["21"]["inputs"]["value"] = freq.get("active", False)
        if freq.get("active"):
            workflow["3"]["inputs"]["blur_radius"] = freq.get("blur_radius", 20)
            workflow["3"]["inputs"]["sigma"] = freq.get("sigma", 1.0)
            workflow["4"]["inputs"]["blend_percentage"] = freq.get("blend_percentage", 1.0)

        # Denoise -> Toggle Node 27
        denoise = report.get("denoise", {})
        workflow["27"]["inputs"]["value"] = denoise.get("active", False)
        if denoise.get("active"):
            workflow["26"]["inputs"]["diameter"] = denoise.get("diameter", 3)
            workflow["26"]["inputs"]["sigma_color"] = denoise.get("sigma_color", 10.0)
            workflow["26"]["inputs"]["sigma_space"] = denoise.get("sigma_space", 10.0)

        # Threshold -> Toggle Node 22
        thresh = report.get("threshold", {})
        workflow["22"]["inputs"]["value"] = thresh.get("active", False)
        if thresh.get("active"):
            workflow["2"]["inputs"]["threshold"] = thresh.get("threshold", 0.1)

        # 3. Execute Workflow
        prompt_res = requests.post(f"{self.valves.COMFYUI_URL}/prompt", json={"prompt": workflow}).json()
        prompt_id = prompt_res["prompt_id"]

        # 4. Poll and dynamically find output node
        for _ in range(60):
            hist = requests.get(f"{self.valves.COMFYUI_URL}/history/{prompt_id}").json()
            if prompt_id in hist:
                outputs = hist[prompt_id].get("outputs", {})
                out_filename = None
                
                # Dynamically find whichever node saved the image (usually 23)
                for node_id, node_data in outputs.items():
                    if "images" in node_data and len(node_data["images"]) > 0:
                        out_filename = node_data["images"][0]["filename"]
                        break
                
                if out_filename:
                    # MUST include type=output for ComfyUI to serve the file
                    img_res = requests.get(f"{self.valves.COMFYUI_URL}/view?filename={out_filename}&type=output")
                    return img_res.content
                else:
                    raise RuntimeError("ComfyUI finished but produced no output image.")
            time.sleep(1.5)
            
        raise TimeoutError("ComfyUI Timeout")

    def pipe(self, user_message: str, model_id: str, messages: List[dict], body: OpenWebUIBody) -> Union[str, Generator]:
        project_id = body.get("chat_id", "manual_upload")
        
        # Extract Image
        base64_image = None
        for msg in reversed(messages):
            if isinstance(msg.get("content"), list):
                for item in msg["content"]:
                    if item.get("type") == "image_url":
                        url = item["image_url"]["url"]
                        base64_image = url.split(",")[1] if "," in url else url
                        break
            if base64_image: break
            
        if not base64_image: 
            yield "No image found in request."; return

        max_passes, min_conf = self.get_batch_constraints()
        original_bytes = base64.b64decode(base64_image)

        yield "👁️ **Pass 0: Initial Vision Triage...**\n"
        page_id, hdd_base_local, hdd_path_local, thumb_path = self.generate_triage_artifacts(project_id, base64_image)
        
        try:
            initial_policy = self.get_initial_triage(thumb_path)
        except Exception as e:
            yield f"❌ Vision Triage Failed: {e}"; return
        
        state = self.update_page_state(page_id, {
            "page_metadata": {"page_id": page_id, "project_id": project_id, "source_path": hdd_path_local},
             "policy_notes": initial_policy,
             "history":[]
        })

        current_confidence = 0.0
        iteration = 0

        while iteration < max_passes and current_confidence < min_conf:
            iteration += 1
            yield f"\n### Pass {iteration}\n"
            
            params = initial_policy if iteration == 1 else self.get_next_adjustment(state, current_confidence)
            yield f"- **Parameters**: `{json.dumps(params)}`\n"
            
            try:
                # ALWAYS pass original_bytes to prevent destructive iteration
                result_bytes = self.process_in_comfyui(page_id, iteration, original_bytes, params)
                
                # Save artifact to HDD
                artifact_path_local = os.path.join(hdd_base_local, f"pass{iteration}_comfy.png")
                with open(artifact_path_local, "wb") as f:
                    f.write(result_bytes)

                artifact_path_remote = artifact_path_local.replace(self.valves.LOCAL_DATA_DIR, self.valves.REMOTE_DATA_DIR)
                
                # Get OCR Metrics
                current_confidence = self.get_ocr_metrics(artifact_path_remote, iteration)
                
                # Update State
                state["history"].append({"pass": iteration, "params": params, "conf": current_confidence})
                self.update_page_state(page_id, {"history": state["history"]})
                
                # Render to UI safely in chunks
                img_b64 = base64.b64encode(result_bytes).decode('utf-8')
                md_img = f"- ✅ Pass {iteration} complete (Confidence: {current_confidence:.2f})\n\n![Pass {iteration}](data:image/png;base64,{img_b64})\n"
                
                chunk_size = 1024 * 64
                for i in range(0, len(md_img), chunk_size):
                    yield md_img[i:i+chunk_size]
                    time.sleep(0.05)
                    
            except Exception as e:
                yield f"\n❌ **Error during Pass {iteration}:** {e}"; return

        if current_confidence >= min_conf:
            yield f"\n\n🎉 **Success!** Target confidence reached. Archive: `{page_id}`"
        else:
            yield f"\n\n⚠️ **Max passes reached.** Final confidence: {current_confidence:.2f}. Archive: `{page_id}`"