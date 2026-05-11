import yt_dlp
import os


def download_video(url: str, output_dir: str) -> str:
    output_path = os.path.join(output_dir, "video.mp4")
    ydl_opts = {
        "outtmpl": output_path,
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
        "merge_output_format": "mp4",
        "quiet": True,
        "no_warnings": True,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        ydl.download([url])
    return output_path
