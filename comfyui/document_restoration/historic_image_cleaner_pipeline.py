import os
import time
import json
import base64
import re
import requests
from typing import List, Union, Generator, Iterator
from pydantic import BaseModel, Field

class Pipeline:
    class Valves(BaseModel):
        OLLAMA_URL: str = "http://192.168.2.86:11434"
        COMFYUI_URL: str = "http://192.168.2.86:8188"
        VISION_MODEL: str = "llama3.2-vision:latest"
        MAX_ITERATIONS: int = 3
        WORKFLOW_JSON_PATH: str = "/app/backend/data/document_restoration_flow.json"

    def __init__(self):
        self.valves = self.Valves()

    def get_vision_report(self, base64_image: str) -> dict:
        prompt = (
            "You are an expert archival document restorer. Analyze this historical document image. "
            "Determine if it needs geometry correction (rotation), frequency correction (noise/bleed-through removal), "
            "or thresholding (contrast/faded ink fixing). "
            "Reply ONLY with a raw JSON object using this exact structure and adhering to the value ranges:\n"
            "{\n"
            "  \"geometry\": {\"active\": true/false, \"rotation_angle\": float (-45.0 to 45.0, positive is clockwise)},\n"
            "  \"frequency\": {\"active\": true/false, \"blur_radius\": int (5 to 50, higher for thicker noise), \"blend_percentage\": float (0.1 to 1.0)},\n"
            "  \"threshold\": {\"active\": true/false, \"level\": float (0.05 to 0.95, lower means darker ink)}\n"
            "}"
        )
        
        payload = {
            "model": self.valves.VISION_MODEL,
            "messages":[
                {
                    "role": "user",
                    "content": prompt,
                    "images":[base64_image]
                }
            ],
            "format": "json",
            "stream": False
        }
        
        try:
            response = requests.post(f"{self.valves.OLLAMA_URL}/api/chat", json=payload).json()
            content = response["message"]["content"]
            
            match = re.search(r'\{.*\}', content, re.DOTALL)
            if match:
                return json.loads(match.group(0))
            return json.loads(content)
        except Exception as e:
            print(f"Vision grading failed: {e}")
            return {
                "geometry": {"active": False},
                "frequency": {"active": False},
                "threshold": {"active": False}
            }

    def process_in_comfyui(self, base64_image: str, report: dict) -> bytes:
        # 1. Upload image to ComfyUI
        img_data = base64.b64decode(base64_image)
        files = {"image": ("temp_doc.jpg", img_data, "image/jpeg")}
        upload_res = requests.post(f"{self.valves.COMFYUI_URL}/upload/image", files=files).json()
        filename = upload_res["name"]

        # 2. Load the API JSON Workflow
        workflow_path = self.valves.WORKFLOW_JSON_PATH
        if not os.path.exists(workflow_path):
            raise FileNotFoundError(f"Workflow file not found at {workflow_path}")
            
        with open(workflow_path, "r") as f:
            workflow = json.load(f)

        # 3. INJECT DYNAMIC PARAMETERS
        workflow["1"]["inputs"]["image"] = filename
        
        geo = report.get("geometry", {})
        is_geo_active = geo.get("active", False)
        workflow["20"]["inputs"]["value"] = is_geo_active 
        if is_geo_active:
            workflow["6"]["inputs"]["rotation"] = float(geo.get("rotation_angle", 0.0))
        
        freq = report.get("frequency", {})
        is_freq_active = freq.get("active", False)
        workflow["21"]["inputs"]["value"] = is_freq_active 
        if is_freq_active:
            workflow["3"]["inputs"]["blur_radius"] = int(freq.get("blur_radius", 20))
            workflow["4"]["inputs"]["blend_percentage"] = float(freq.get("blend_percentage", 1.0))
        
        thresh = report.get("threshold", {})
        is_thresh_active = thresh.get("active", False)
        workflow["22"]["inputs"]["value"] = is_thresh_active 
        if is_thresh_active:
            workflow["2"]["inputs"]["threshold"] = float(thresh.get("level", 0.1))

        # 4. Trigger the ComfyUI Generation
        req_data = {"prompt": workflow}
        prompt_res = requests.post(f"{self.valves.COMFYUI_URL}/prompt", json=req_data).json()
        
        if "error" in prompt_res:
            err_msg = prompt_res["error"].get("message", "Unknown error")
            raise RuntimeError(f"ComfyUI rejected the prompt: {err_msg}")

        prompt_id = prompt_res["prompt_id"]

        # 5. Poll for completion safely
        max_retries = 60 
        retries = 0
        while retries < max_retries:
            try:
                history_res = requests.get(f"{self.valves.COMFYUI_URL}/history/{prompt_id}")
                if history_res.status_code == 200:
                    history = history_res.json()
                    if prompt_id in history:
                        # Safely check for outputs to avoid KeyErrors
                        outputs = history[prompt_id].get("outputs", {})
                        if "23" in outputs and "images" in outputs["23"]:
                            out_filename = outputs["23"]["images"][0]["filename"]
                            img_res = requests.get(f"{self.valves.COMFYUI_URL}/view?filename={out_filename}")
                            if img_res.status_code == 200:
                                return img_res.content
                            else:
                                raise RuntimeError("Failed to download image from ComfyUI.")
                        elif "error" in history[prompt_id]:
                            raise RuntimeError(f"ComfyUI Node Error: {history[prompt_id]['error']}")
                        else:
                            raise RuntimeError("ComfyUI finished but Node 23 produced no images.")
            except requests.exceptions.RequestException:
                pass # Ignore temporary network blips while polling
            
            time.sleep(1.5)
            retries += 1
            
        raise TimeoutError("ComfyUI generation timed out.")

    def pipe(self, user_message: str, model_id: str, messages: List[dict], body: dict) -> Union[str, Generator, Iterator]:
        base64_image = None
        try:
            for msg in reversed(messages):
                if isinstance(msg.get("content"), list):
                    for item in msg["content"]:
                        if item.get("type") == "image_url":
                            url = item["image_url"]["url"]
                            # Safely split the base64 string
                            base64_image = url.split(",")[1] if "," in url else url
                            break
                if base64_image:
                    break
        except Exception as e:
            yield f"❌ Error reading uploaded image: {e}"
            return

        if not base64_image:
            yield "Please upload a document image for me to process."
            return

        current_image = base64_image
        iteration = 0
        is_clean = False

        yield "🔍 **Starting Dynamic Document Restoration Pipeline**...\n"

        while iteration < self.valves.MAX_ITERATIONS and not is_clean:
            iteration += 1
            yield f"\n### Iteration {iteration}\n"
            yield "- 👀 AI is analyzing document and calculating parameters...\n"
            
            report = self.get_vision_report(current_image)
            yield f"- **Calculated Parameters**: `{json.dumps(report, indent=2)}`\n"

            if not any(category.get("active", False) for category in report.values()):
                yield "- ✅ AI reports document is clean and requires no further adjustments! Ending loop.\n"
                is_clean = True
                break

            yield "- ⚙️ Injecting parameters and routing through ComfyUI...\n"
            
            try:
                result_img_bytes = self.process_in_comfyui(current_image, report)
            except Exception as e:
                yield f"\n❌ **Pipeline Error:** {str(e)}\n"
                return
                
            current_image = base64.b64encode(result_img_bytes).decode('utf-8')
            
            yield "- ✅ ComfyUI processing complete. Rendering image...\n\n"
            
            # CRITICAL FIX: Chunk the massive base64 string so the server doesn't crash
            markdown_img = f"![Processed Document](data:image/jpeg;base64,{current_image})\n"
            chunk_size = 1024 * 64 # Yield in 64KB chunks
            for i in range(0, len(markdown_img), chunk_size):
                yield markdown_img[i:i+chunk_size]
                time.sleep(0.05) # Give the network buffer time to flush
            
            yield "\n- Passing back to Vision Model for re-evaluation...\n"

        if not is_clean:
            yield f"\n⚠️ Reached maximum iterations ({self.valves.MAX_ITERATIONS}). Sending to human review.\n"

        yield "\n🎉 **Restoration Complete.** Ready for OCR."
