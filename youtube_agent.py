#!/usr/bin/env python3
import argparse
import json
import io
import os
import re
import shutil
import subprocess
import sys
import random
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import requests
from duckduckgo_search import DDGS
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# YouTube API imports
import googleapiclient.discovery
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload

@dataclass
class AgentConfig:
    llm_url: str
    llm_model: str
    llm_api_key: Optional[str]
    coqui_url: str
    bark_url: str
    output_root: Path
    topics_path: Path
    state_path: Path
    token_path: Path
    logo_path: Path
    intro_path: Path
    pexels_key: str = "YOUR_PEXELS_API_KEY_HERE"
    speaker: str = "baldur_sanjin"
    speaker_wav: Optional[str] = None
    language: str = "en"

def slugify(value: str) -> str:
    value = str(value).strip().lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    return value.strip("-") or "investment"

def load_json(path: Path, fallback: Any) -> Any:
    if not path.exists(): return fallback
    return json.loads(path.read_text(encoding="utf-8"))

def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

def llm_generate(cfg: AgentConfig, prompt: str, json_mode: bool = False) -> str:
    headers = {"Content-Type": "application/json"}
    if cfg.llm_api_key:
        headers["Authorization"] = f"Bearer {cfg.llm_api_key}"
        
    payload = {
        "model": cfg.llm_model,
        "messages": [{"role": "system", "content": "You are a helpful assistant."}, {"role": "user", "content": prompt}],
        "stream": False
    }
    
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
        
    resp = requests.post(cfg.llm_url, json=payload, headers=headers, timeout=1200)
    resp.raise_for_status()
    data = resp.json()
    
    # Handle both OpenAI chat schema and bare fallbacks
    if "choices" in data and len(data["choices"]) > 0:
        return (data["choices"][0].get("message", {}).get("content") or "").strip()
    return (data.get("response") or "").strip()

def update_status(phase: str, current: int = 0, total: int = 0, message: str = ""):
    status_path = Path("./progress.json")
    data = {
        "phase": phase,
        "current": current,
        "total": total,
        "message": message,
        "timestamp": time.time()
    }
    status_path.write_text(json.dumps(data), encoding="utf-8")

def log_history(title: str, video_id: str, thumbnail: str):
    history_path = Path("./history.json")
    history = []
    if history_path.exists():
        try: history = json.loads(history_path.read_text(encoding="utf-8"))
        except: pass
    
    history.insert(0, {
        "title": title,
        "url": f"https://youtube.com/watch?v={video_id}",
        "thumbnail": thumbnail,
        "timestamp": datetime.now().isoformat()
    })
    history_path.write_text(json.dumps(history, indent=2), encoding="utf-8")

def parse_json_with_fallback(raw_text: str, fallback: Any) -> Any:
    try:
        clean_text = re.sub(r"```json\s*|\s*```", "", raw_text).strip()
        return json.loads(clean_text)
    except: return fallback

def split_into_sentences(text: str) -> List[str]:
    # Same as story agent
    paragraphs = text.split('\n')
    all_sentences = []
    for para in paragraphs:
        if not para.strip(): continue
        sentences = re.split(r'(?<=[.!?])\s+', para.strip())
        all_sentences.extend(sentences)
    final = []
    for s in all_sentences:
        s = s.strip()
        if not s: continue
        if len(s) > 200:
            words = s.split(' ')
            cur = []
            for w in words:
                if len(' '.join(cur + [w])) > 200:
                    final.append(' '.join(cur)); cur = [w]
                else: cur.append(w)
            if cur: final.append(' '.join(cur))
        else: final.append(s)
    return final

def synthesize_audio(cfg: AgentConfig, text: str, output_wav: Path) -> bool:
    # Reuse logic from story_agent.py
    chunks = split_into_sentences(text)
    temp_wavs = []
    try:
        for i, chunk in enumerate(chunks):
            chunk_wav = output_wav.parent / f"{output_wav.stem}_chunk_{i}.wav"
            success = False
            urls = [cfg.coqui_url.rstrip("/") + "/tts"]
            for url in urls:
                try:
                    payload = {"text": chunk, "language": cfg.language}
                    if cfg.speaker_wav:
                        payload["speaker_wav"] = cfg.speaker_wav
                    else:
                        payload["speaker"] = cfg.speaker
                        
                    r = requests.post(url, json=payload, timeout=300)
                    if r.status_code < 400 and "audio" in r.headers.get("Content-Type", ""):
                        chunk_wav.write_bytes(r.content); success = True; break
                except: continue
            if not success:
                try:
                    r = requests.post(f"{cfg.bark_url.rstrip('/')}/generate", json={"text": chunk}, timeout=300)
                    if r.status_code < 400:
                        dl = requests.get(f"{cfg.bark_url.rstrip('/')}/download/{r.json().get('file_id')}", timeout=120)
                        chunk_wav.write_bytes(dl.content); success = True
                except: pass
            if success: temp_wavs.append(chunk_wav)
            else: return False
        if not temp_wavs: return False
        input_list = output_wav.with_suffix(".txt")
        input_list.write_text("\n".join(f"file '{p.absolute()}'" for p in temp_wavs))
        subprocess.run(["ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", str(input_list), "-c", "copy", str(output_wav)], check=True, capture_output=True)
        input_list.unlink()
        return True
    finally:
        for p in temp_wavs: 
            if p.exists(): p.unlink()

def probe_duration(file_path: Path) -> float:
    if not file_path.exists(): return 0.0
    cmd = ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(file_path)]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
        return float(res.stdout.strip() or 0.0)
    except: return 0.0

def fetch_pexels_visual(query: str, output_path_base: Path, cfg: AgentConfig) -> bool:
    """Fetch a relevant video or image from Pexels, fallback to DuckDuckGo."""
    headers = {"Authorization": cfg.pexels_key}
    encoded_query = requests.utils.quote(query)
    
    # 1. Try Pexels Video
    try:
        print(f"    Searching Pexels Video: '{query}'")
        v_url = f"https://api.pexels.com/videos/search?query={encoded_query}&per_page=5&orientation=landscape"
        vr = requests.get(v_url, headers=headers, timeout=15)
        if vr.status_code == 200:
            videos = vr.json().get('videos', [])
            if videos:
                video = random.choice(videos)
                video_files = video.get('video_files', [])
                # Get highest quality HD video
                hd_files = [f for f in video_files if f.get('quality') == 'hd']
                best_file = hd_files[0] if hd_files else (video_files[0] if video_files else None)
                
                if best_file and best_file.get('link'):
                    dl = requests.get(best_file['link'], timeout=30)
                    if dl.status_code == 200 and len(dl.content) > 10000:
                        out_mp4 = output_path_base.with_suffix('.mp4')
                        out_mp4.write_bytes(dl.content)
                        print(f"    Downloaded Pexels Video successfully.")
                        return True
    except Exception as e:
        print(f"    Pexels Video failed: {e}")

    # 2. Try Pexels Image
    try:
        print(f"    Searching Pexels Image: '{query}'")
        i_url = f"https://api.pexels.com/v1/search?query={encoded_query}&per_page=5&orientation=landscape"
        ir = requests.get(i_url, headers=headers, timeout=15)
        if ir.status_code == 200:
            photos = ir.json().get('photos', [])
            if photos:
                photo = random.choice(photos)
                src = photo.get('src', {})
                img_url = src.get('large2x') or src.get('large') or src.get('original')
                if img_url:
                    dl = requests.get(img_url, timeout=15)
                    if dl.status_code == 200 and len(dl.content) > 5000:
                        img = Image.open(io.BytesIO(dl.content)).convert("RGB")
                        img.save(output_path_base.with_suffix('.jpg'), "JPEG", quality=95)
                        print(f"    Downloaded Pexels Image successfully.")
                        return True
    except Exception as e:
        print(f"    Pexels Image failed: {e}")

    # 3. Fallback DuckDuckGo Image
    search_variants = [f"{query} infographic", f"{query} abstract diagram"]
    for search_query in search_variants:
        try:
            print(f"    Fallback DDG search: '{search_query}'")
            results = DDGS().images(keywords=search_query, max_results=3, safesearch="moderate")
            for result in results:
                img_url = result.get("image", "")
                if not img_url: continue
                try:
                    r = requests.get(img_url, timeout=10, headers={"User-Agent": "Mozilla/5.0"})
                    if r.status_code < 400 and len(r.content) > 5000:
                        img = Image.open(io.BytesIO(r.content)).convert("RGB")
                        if img.width < 400: continue
                        img.save(output_path_base.with_suffix('.jpg'), "JPEG", quality=95)
                        print(f"    Downloaded DDG Image.")
                        return True
                except: continue
        except: continue
    return False


def create_infographic_slide(bullets: List[str], logo_path: Path, output_path: Path,
                             bg_image_path: Optional[Path] = None, transparent_bg: bool = False):
    """Create a slide with an infographic background, dark overlay, and bullet text."""
    W, H = 1920, 1080

    # --- Background ---
    if transparent_bg:
        img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    elif bg_image_path and bg_image_path.exists():
        try:
            bg = Image.open(bg_image_path).convert("RGB")
            # Resize to cover: scale up to fill, then center-crop
            scale = max(W / bg.width, H / bg.height)
            new_w, new_h = int(bg.width * scale), int(bg.height * scale)
            bg = bg.resize((new_w, new_h), Image.LANCZOS)
            left = (new_w - W) // 2
            top = (new_h - H) // 2
            bg = bg.crop((left, top, left + W, top + H))
            # Slight blur to keep text readable
            bg = bg.filter(ImageFilter.GaussianBlur(radius=2))
            img = bg
        except Exception:
            img = Image.new("RGB", (W, H), (20, 25, 45))  # Dark navy fallback
    else:
        img = Image.new("RGB", (W, H), (20, 25, 45))  # Dark navy fallback

    # --- Semi-transparent dark overlay on the bottom 35% ---
    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    overlay_draw = ImageDraw.Draw(overlay)
    overlay_top = int(H * 0.58)
    overlay_draw.rectangle([(0, overlay_top), (W, H)], fill=(0, 0, 0, 180))
    # Also add a subtle top strip for the logo
    overlay_draw.rectangle([(0, 0), (W, 90)], fill=(0, 0, 0, 120))
    img = img.convert("RGBA")
    img = Image.alpha_composite(img, overlay)
    if not transparent_bg:
        img = img.convert("RGB")

    draw = ImageDraw.Draw(img)

    # --- Logo (top-left, safe from zoompan crop) ---
    if logo_path.exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((70, 70))
            # Create a temp RGBA canvas to paste onto
            logo_canvas = Image.new("RGBA", img.size, (0, 0, 0, 0))
            logo_canvas.paste(logo, (20, 10), logo if logo.mode == "RGBA" else None)
            img = Image.alpha_composite(img.convert("RGBA"), logo_canvas)
            if not transparent_bg:
                img = img.convert("RGB")
            draw = ImageDraw.Draw(img)
        except Exception:
            pass

    # --- Fonts ---
    try:
        font_bullet = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 42)
    except Exception:
        font_bullet = ImageFont.load_default()

    # --- Word wrap helper ---
    def wrap_text(text: str, font, max_width: int) -> List[str]:
        lines = []
        words = text.split(" ")
        current_line: List[str] = []
        for word in words:
            test_line = " ".join(current_line + [word])
            if draw.textlength(test_line, font=font) < max_width:
                current_line.append(word)
            else:
                if current_line:
                    lines.append(" ".join(current_line))
                current_line = [word]
        if current_line:
            lines.append(" ".join(current_line))
        return lines

    # --- Draw bullet points in the overlay area ---
    y = overlay_top + 30
    max_w = W - 200  # 100px margins on each side
    for bullet in bullets:
        wrapped = wrap_text(f"▸ {bullet}", font_bullet, max_w)
        for line in wrapped:
            if y > H - 30:
                break
            # Text shadow for extra readability
            draw.text((102, y + 2), line, fill=(0, 0, 0), font=font_bullet)
            draw.text((100, y), line, fill=(255, 255, 255), font=font_bullet)
            y += 55
        y += 15  # space between bullets
        if y > H - 30:
            break

    if transparent_bg:
        img.save(output_path, "PNG")
    else:
        img.save(output_path, "JPEG", quality=95)


def create_bullet_slide(bullets: List[str], logo_path: Path, output_path: Path):
    """Fallback: plain dark slide with bullets (no background image)."""
    create_infographic_slide(bullets, logo_path, output_path, bg_image_path=None)


def generate_thumbnail(title: str, logo_path: Path, output_path: Path):
    W, H = 1280, 720
    img = Image.new("RGB", (W, H), (20, 25, 45))
    draw = ImageDraw.Draw(img)

    # Logo — top-left
    if logo_path.exists():
        try:
            logo = Image.open(logo_path).convert("RGBA")
            logo.thumbnail((200, 200))
            canvas = Image.new("RGBA", img.size, (0, 0, 0, 0))
            canvas.paste(logo, (40, 40), logo if logo.mode == "RGBA" else None)
            img = Image.alpha_composite(img.convert("RGBA"), canvas).convert("RGB")
            draw = ImageDraw.Draw(img)
        except Exception:
            pass

    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 72)
    except Exception:
        font = ImageFont.load_default()

    # Wrap title
    words = title.split()
    lines: List[str] = []
    cur = ""
    for w in words:
        if len(cur + w) < 25:
            cur += w + " "
        else:
            lines.append(cur.strip())
            cur = w + " "
    if cur:
        lines.append(cur.strip())

    y = 350
    for line in lines:
        draw.text((102, y + 2), line, fill=(0, 0, 0), font=font)  # shadow
        draw.text((100, y), line, fill=(255, 255, 255), font=font)
        y += 90

    img.save(output_path, "JPEG", quality=95)


def generate_video(cfg: AgentConfig, scenes: List[Dict[str, Any]], struct_title: str, output_dir: Path) -> Path:
    assets_dir = output_dir / "assets"
    assets_dir.mkdir(exist_ok=True)
    video_clips = []

    for i, scene in enumerate(scenes):
        if not isinstance(scene, dict):
            continue
        sid = f"scene_{i}"
        audio_path = assets_dir / f"{sid}.wav"
        img_path = assets_dir / f"{sid}.jpg"
        bg_img_path = assets_dir / f"{sid}_bg.jpg"
        scene_video = assets_dir / f"{sid}.mp4"

        # RESUME LOGIC: Check if this scene is already complete
        if scene_video.exists() and probe_duration(scene_video) > 0:
            print(f"  Skipping {sid} (already exists)...")
            video_clips.append(scene_video)
            continue

        narration = scene.get("narration", "")
        if not narration:
            continue
        
        update_status("Content Generation", i + 1, len(scenes), f"Synthesizing audio for scene {i+1}...")
        if not synthesize_audio(cfg, narration, audio_path):
            continue
        duration = probe_duration(audio_path)

        bullets = scene.get("bullet_points", [])
        if not bullets:
            bullets = [struct_title]

        # Fetch a relevant infographic image for this scene
        image_query = scene.get("image_query", "")
        if not image_query:
            image_query = f"{struct_title} finance"
        
        update_status("Content Generation", i + 1, len(scenes), f"Fetching visuals for scene {i+1}...")
        bg_video_path = bg_img_path.with_suffix('.mp4')
        if bg_video_path.exists(): bg_video_path.unlink()
        if bg_img_path.exists(): bg_img_path.unlink()
        
        has_bg = fetch_pexels_visual(image_query, bg_img_path, cfg)
        is_video_bg = bg_video_path.exists()
        
        if is_video_bg:
            # Create a transparent PNG with just the overlay and text
            png_path = img_path.with_suffix('.png')
            create_infographic_slide(bullets, cfg.logo_path, png_path, transparent_bg=True)
            
            # Loop video, overlay PNG, sync with audio
            overlay_cmd = [
                "ffmpeg", "-y",
                "-stream_loop", "-1", "-i", str(bg_video_path),
                "-i", str(png_path),
                "-i", str(audio_path),
                "-filter_complex", "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,setsar=1[bg];[bg][1:v]overlay=0:0[outv];[2:a]aformat=sample_fmts=fltp:sample_rates=44100:channel_layouts=stereo[aout]",
                "-map", "[outv]", "-map", "[aout]",
                "-c:v", "libx264", "-t", str(duration), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
                str(scene_video)
            ]
            update_status("Rendering", i + 1, len(scenes), f"Compositing video scene {i+1}...")
            subprocess.run(overlay_cmd, check=True, capture_output=True)
        else:
            create_infographic_slide(
                bullets, cfg.logo_path, img_path,
                bg_image_path=bg_img_path if has_bg else None
            )

            # Create animated scene video with Zoom/Pan effect
            num_frames = int(duration * 25)
            zoom_cmd = [
                "ffmpeg", "-y",
                "-loop", "1", "-i", str(img_path),
                "-i", str(audio_path),
                "-vf", f"zoompan=z='min(zoom+0.0005,1.1)':d={num_frames}:s=1920x1080,fps=25",
                "-c:v", "libx264", "-t", str(duration), "-pix_fmt", "yuv420p",
                "-c:a", "aac", "-b:a", "192k", "-shortest",
                str(scene_video),
            ]
            update_status("Rendering", i + 1, len(scenes), f"Animating scene {i+1} ({duration:.2f}s)...")
            subprocess.run(zoom_cmd, check=True, capture_output=True)
            
        video_clips.append(scene_video)

    if not video_clips:
        raise RuntimeError("No video clips were generated.")

    # Concat all scene videos
    concat_list = output_dir / "video_list.txt"
    concat_list.write_text("\n".join(f"file '{p.absolute()}'" for p in video_clips))

    output_video = output_dir / "youtube_explanation.mp4"
    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0", "-i", str(concat_list),
        "-c", "copy",
        str(output_video),
    ]
    subprocess.run(cmd, check=True, capture_output=True)
    
    # Audio Ducking integration
    bg_music_path = Path("bg_music.mp3")
    if bg_music_path.exists():
        ducked_video = output_dir / "ducked.mp4"
        duck_cmd = [
            "ffmpeg", "-y",
            "-i", str(output_video),
            "-stream_loop", "-1", "-i", str(bg_music_path),
            "-filter_complex", 
            "[1:a]volume=0.2[bg];[0:a]asplit[main][sc];[bg][sc]sidechaincompress=threshold=0.03:ratio=4:attack=50:release=100[bg_ducked];[main][bg_ducked]amix=inputs=2:duration=first:dropout_transition=2:normalize=0[aout]",
            "-map", "0:v", "-map", "[aout]",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
            str(ducked_video)
        ]
        try:
            print("  Applying background music with auto-ducking...")
            update_status("Rendering", len(scenes), len(scenes), "Applying auto-ducked music...")
            subprocess.run(duck_cmd, check=True, capture_output=False)
            if ducked_video.exists():
                import shutil
                shutil.move(str(ducked_video), str(output_video))
        except Exception as e:
            print(f"  Warning: Audio ducking failed. {e}")
            
    return output_video

def prepend_intro(intro_path: Path, content_path: Path, output_path: Path):
    print(f"  Prepending intro: {intro_path.name}")
    # Normalize both to 1080p and same audio format for seamless merge
    filter_complex = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(ih-ih)/2,setsar=1[v0]; "
        "[1:v]scale=1920:1080:force_original_aspect_ratio=decrease,pad=1920:1080:(ow-iw)/2:(ih-ih)/2,setsar=1[v1]; "
        "[0:a]aformat=sample_rates=44100:channel_layouts=stereo[a0]; "
        "[1:a]aformat=sample_rates=44100:channel_layouts=stereo[a1]; "
        "[v0][a0][v1][a1]concat=n=2:v=1:a=1[v][a]"
    )
    cmd = [
        "ffmpeg", "-y",
        "-i", str(intro_path),
        "-i", str(content_path),
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
        str(output_path)
    ]
    subprocess.run(cmd, check=True)

def upload_to_youtube(cfg: AgentConfig, mp4_path: Path, thumbnail_path: Path, metadata: Dict[str, Any]):
    scopes = ["https://www.googleapis.com/auth/youtube.upload"]
    if not cfg.token_path.exists():
        raise RuntimeError(f"Investment token not found at {cfg.token_path}. Run auth_youtube_investment.py first.")
    
    creds = Credentials.from_authorized_user_file(str(cfg.token_path), scopes)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    
    youtube = googleapiclient.discovery.build("youtube", "v3", credentials=creds)
    
    # Construct an SEO-friendly description with sponsor blurbs
    clean_desc = metadata.get("description", "")
    if isinstance(clean_desc, list): clean_desc = "\n".join(clean_desc)
    clean_desc = str(clean_desc).strip("[] \n\t\r").strip('\"')
    
    video_description = (
        f"{clean_desc}\n\n"
        "--- SPONSORS ---\n"
        "Togetherbudget.com: Your ultimate personal finance companion. Master your budget, track your wealth, and reach financial freedom. Visit https://www.togetherbudget.com to start your journey today.\n\n"
        "Powered by Malik Abualzait AI Agents Network: The future of autonomous intelligence. Empowering businesses and individuals through advanced AI solutions. Visit https://mabualzait.com to learn more.\n\n"
        "--- ABOUT THIS VIDEO ---\n"
        f"This investment strategy deep dive on '{metadata.get('title')}' is designed to help you navigate the complex world of finance with clarity and confidence."
    )
    
    body = {
        "snippet": {
            "title": str(metadata.get("title", "Investment Strategy"))[:100],
            "description": video_description,
            "tags": metadata.get("tags", ["investing", "finance"]),
            "categoryId": "27"
        },
        "status": {"privacyStatus": "public"}
    }
    
    media = MediaFileUpload(str(mp4_path), chunksize=-1, resumable=True)
    request = youtube.videos().insert(part="snippet,status", body=body, media_body=media)
    response = request.execute()
    video_id = response.get('id')
    print(f"Uploaded! ID: {video_id}")
    
    # Log to history
    log_history(metadata.get("title", "Investment Strategy"), video_id, str(thumbnail_path.name))

    if thumbnail_path.exists():
        print("Uploading thumbnail...")
        youtube.thumbnails().set(
            videoId=video_id,
            media_body=MediaFileUpload(str(thumbnail_path))
        ).execute()

def discover_new_topics(cfg: AgentConfig, existing_titles: List[str]):
    print("  Discovering new investment topics...")
    prompt = f"Suggest 5 trending investment topics for 2026. Avoid these already covered: {existing_titles}. Return JSON array of objects with 'topic' and 'description'."
    raw_res = llm_generate(cfg, prompt, json_mode=True)
    new_topics = parse_json_with_fallback(raw_res, [])
    if isinstance(new_topics, list) and new_topics:
        return new_topics
    return []

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--topic", type=str, help="Override topic string directly")
    parser.add_argument("--topic-idx", type=int)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--speaker-wav", type=str, help="Path to reference WAV inside the Docker container for voice cloning")
    args = parser.parse_args()

    # Load custom config if exists
    config_path = Path("./config.json")
    custom_config = load_json(config_path, {})
    custom_pexels = custom_config.get("pexels_key", "YOUR_PEXELS_API_KEY_HERE")
    if not custom_pexels:
        custom_pexels = "YOUR_PEXELS_API_KEY_HERE"

    cfg = AgentConfig(
        llm_url=custom_config.get("llm_url", "http://localhost:11434/v1/chat/completions"),
        llm_model=custom_config.get("llm_model", "llama3.1:8b"),
        llm_api_key=custom_config.get("llm_api_key", ""),
        coqui_url="http://localhost:8124",
        bark_url="http://localhost:8001",
        output_root=Path("./output_youtube"),
        topics_path=Path("./topics.json"),
        state_path=Path("./state.json"),
        token_path=Path("./token_youtube.json"),
        logo_path=Path("./logo.jpeg"),
        intro_path=Path("./intro.mp4"),
        speaker="baldur_sanjin", # Clear, energetic narration style
        pexels_key=custom_pexels,
        speaker_wav=args.speaker_wav,
        language="en"
    )

    topics = load_json(cfg.topics_path, [])
    state = load_json(cfg.state_path, {"next": 0})
    
    if args.topic:
        topic_data = {"topic": args.topic}
        idx = 0
    else:
        # Sustainability: check if we need more topics
        if state["next"] >= len(topics):
            existing_titles = [t['topic'] for t in topics]
            new_batch = discover_new_topics(cfg, existing_titles)
            if new_batch:
                topics.extend(new_batch)
                save_json(cfg.topics_path, topics)
                print(f"  Added {len(new_batch)} new topics to the list.")
        
        idx = args.topic_idx if args.topic_idx is not None else state.get("next", 0) % len(topics)
        topic_data = topics[idx]
    
    # Predict output folder name for resume logic
    struct_slug = slugify(topic_data['topic'])
    out_dir = cfg.output_root / struct_slug
    
    # If already fully complete, we can either skip or increment
    if (out_dir / "final_youtube_video.mp4").exists() or (out_dir / "youtube_explanation.mp4").exists():
        print(f"  {topic_data['topic']} already completed! To re-render, delete the folder: {out_dir}")
        if not args.topic:
            state["next"] = idx + 1
            save_json(cfg.state_path, state)
        return

    print(f"Generating explanation for: {topic_data['topic']}")
    update_status("Planning", 0, 1, f"Drafting structure for {topic_data['topic']}...")
    
    # Structure Prompt
    # Structure Prompt: Requesting 10-12 subtopics for >10 min duration
    struct_prompt = f"Plan a comprehensive 15-20 min masterclass explanation for {topic_data['topic']}. Return JSON with 'title', 'subtopics' (list of 10-12 themes)."
    raw_struct = llm_generate(cfg, struct_prompt, json_mode=True)
    struct = parse_json_with_fallback(raw_struct, {"title": topic_data['topic'], "subtopics": ["Overview", "Mechanics", "Pros", "Cons", "Conclusion"]})
    


    
    all_scenes = []
    for i, sub in enumerate(struct['subtopics']):
        sub_name = sub.get('title', sub.get('name', str(sub))) if isinstance(sub, dict) else str(sub)
        print(f"  Drafting sub-topic: {sub_name}")
        update_status("Planning", i + 1, len(struct['subtopics']), f"Generating details for {sub_name}...")
        scene_prompt = (
            f"Explain '{sub_name}' in the context of {struct['title']}. Generate 4 detailed scenes. "
            "Return JSON array of objects with 'narration' (90-100 words), 'bullet_points' (list of 3 concise points), "
            "and 'image_query' (a short 3-5 word search query to find a relevant infographic or illustration for this scene, e.g. 'budget pie chart infographic'). "
            "IMPORTANT: Do NOT include labels like 'Scene 1', 'Narration:', or '(Scene 1)' in the narration text itself. "
            "The narration should start directly with the content."
        )
        raw_scenes = llm_generate(cfg, scene_prompt, json_mode=True)
        scenes = parse_json_with_fallback(raw_scenes, [])
        if isinstance(scenes, dict):
            for k in scenes: 
                if isinstance(scenes[k], list): scenes = scenes[k]; break
        if isinstance(scenes, list):
            for s in scenes:
                if isinstance(s, dict) and s.get('narration'):
                    # Strip out "Scene X:", "Narration:", or "[Scene X]"
                    narration = str(s['narration'])
                    narration = re.sub(r"^(Scene\s*\d+\s*[:\-]*\s*|Narration\s*[:\-]*\s*|\[Scene\s*\d+\]\s*)", "", narration, flags=re.IGNORECASE).strip()
                    s['narration'] = narration
                    all_scenes.append(s)

    if all_scenes:
        intro_text = "This video is brought to you by Togetherbudget.com. "
        all_scenes[0]['narration'] = intro_text + all_scenes[0]['narration']

    if args.dry_run:
        print(f"DRY RUN COMPLETE: {len(all_scenes)} scenes generated for '{struct['title']}'.")
        # Save a preview of the script
        preview = "\n\n".join([f"--- Scene {i+1} ---\n{s.get('narration','')}\nBullets: {s.get('bullet_points',[])}\nImage Query: {s.get('image_query','')}" for i, s in enumerate(all_scenes)])
        print(f"--- Script Preview ---\n{preview[:3000]}...")
        return

    # ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    # out_dir = cfg.output_root / f"{ts}_{slugify(struct['title'])}"
    out_dir.mkdir(parents=True, exist_ok=True)
    
    video_path = generate_video(cfg, all_scenes, struct['title'], out_dir)
    
    # Prepend Intro
    if cfg.intro_path.exists():
        final_video_path = out_dir / "final_youtube_video.mp4"
        prepend_intro(cfg.intro_path, video_path, final_video_path)
        video_path = final_video_path
        
    thumbnail_path = out_dir / "thumbnail.jpg"
    generate_thumbnail(struct['title'], cfg.logo_path, thumbnail_path)
    
    print("Generating YouTube metadata...")
    meta_prompt = (
        f"Generate a high-conversion YouTube Title, a 2-paragraph SEO-friendly Description that summarizes the key takeaways, "
        f"and 15 highly searched tags for a video about {struct['title']}. "
        "Return the response in JSON format with keys: 'title', 'description', 'tags'."
    )
    metadata = parse_json_with_fallback(llm_generate(cfg, meta_prompt, json_mode=True), {"title": struct['title']})
    
    try:
        update_status("YouTube", 0, 1, "Uploading final masterclass...")
        upload_to_youtube(cfg, video_path, thumbnail_path, metadata)
        update_status("Complete", 1, 1, f"Video production finished: {struct['title']}")
        
        # Advance state ONLY on success
        state["next"] = idx + 1
        save_json(cfg.state_path, state)

        # Cleanup Assets
        assets_dir = out_dir / "assets"
        if assets_dir.exists():
            shutil.rmtree(assets_dir)
            print("  Cleaned up assets folder.")
        # Remove intermediate content video if intro was added
        content_video = out_dir / "youtube_explanation.mp4"
        if cfg.intro_path.exists() and content_video.exists():
            content_video.unlink()
            
    except Exception as e:
        print(f"Upload failed: {e}")

if __name__ == "__main__":
    main()
