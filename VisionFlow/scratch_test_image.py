from config import get_settings
from core.media_api import MediaAPIClient

settings = get_settings()
media = MediaAPIClient(
    api_key=settings.gkapi_key,
    base_url=settings.gkapi_baseurl,
)

try:
    print(f"Testing qwen-image with 720p and aspect_ratio 9:16...")
    task_id = media.generate_image(
        prompt="A beautiful landscape",
        model="qwen-image",
        size="720p",
        aspect_ratio="9:16",
    )
    print(f"Success! Task ID:", task_id)
except Exception as e:
    print(f"Failed:", e)
