#!/usr/bin/env python3
"""Вставляет баннер MusorDrop в середину ролика.

Использование:
    python musordrop_insert.py ролик.mp4 [-o выход.mp4] [--banner баннер.mp4]

Правила (канал Musor TikTok и поддержка, актуально на 02.10.2026):
  * ролик до 2 минут: один баннер, середина итогового ролика (с учётом рекламы)
    должна попадать в баннер, допуск 2-3 с;
  * звук баннера обязателен, ускорение не больше 1.2x (6 с -> 5 с);
  * баннер занимает не меньше 25% экрана (берём 28% с запасом);
  * если в ролике говорят, основное видео ставится на паузу.

Как собирается: рез в самой тихой точке у нужной секунды, основное видео и звук
стоят, фон - размытый затемнённый стоп-кадр, поверх него баннер по центру
(зелёный фон вырезается), громкость баннера подгоняется под громкость ролика.

Нужен только ffmpeg/ffprobe в PATH и Python 3.8+.
"""

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
from array import array
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_BANNER = HERE / "assets" / "musordrop_banner.mp4"

SPEED = 1.2              # максимум, который разрешает MusorDrop
MIN_COVERAGE = 0.25      # требование спонсора
TARGET_COVERAGE = 0.28   # берём с запасом
MAX_LEN_ONE_BANNER = 120.0
EDGE_MARGIN = 0.75       # середина ролика не ближе этого к краям баннера, с

# Видимая панель внутри кадра баннера 1902x1000 (остальное - зелёный фон).
PANEL_X0, PANEL_X1 = 0, 1901
PANEL_Y0, PANEL_Y1 = 80, 919


def run(cmd, **kw):
    return subprocess.run(cmd, check=True, **kw)


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
               "-show_streams", str(path)], capture_output=True, text=True).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    num, den = v.get("avg_frame_rate", "30/1").split("/")
    fps = float(num) / float(den) if float(den) else 30.0
    if not 10 <= fps <= 120:
        fps = 30.0
    w, h = int(v["width"]), int(v["height"])
    rot = int(v.get("tags", {}).get("rotate", 0) or 0)
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", rot) or rot)
    if abs(rot) % 180 == 90:
        w, h = h, w
    return {"w": w, "h": h, "fps": fps, "dur": float(info["format"]["duration"]),
            "audio": has_audio}


def loudness(path):
    """Интегральная громкость, LUFS (None, если тишина или нет звука)."""
    p = run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
             "-af", "loudnorm=print_format=json", "-f", "null", "-"],
            capture_output=True, text=True)
    m = re.search(r'"input_i"\s*:\s*"(-?[\d.inf]+)"', p.stderr)
    if not m:
        return None
    val = float(m.group(1))
    return val if math.isfinite(val) and val > -70 else None


def quietest_point(path, lo, hi, ideal, rate=8000, win=0.02, smooth=0.12):
    """Самая тихая точка в [lo, hi]; при равной тишине ближе к ideal."""
    pcm = run(["ffmpeg", "-v", "error", "-ss", f"{max(lo - smooth, 0):.3f}",
               "-t", f"{hi - lo + 2 * smooth:.3f}", "-i", str(path), "-vn",
               "-ac", "1", "-ar", str(rate), "-f", "s16le", "-"],
              capture_output=True).stdout
    samples = array("h")
    samples.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        samples.byteswap()
    start = max(lo - smooth, 0)
    n = int(rate * win)
    energy = [sum(x * x for x in samples[i:i + n]) / n
              for i in range(0, len(samples) - n + 1, n)]
    if not energy:
        return ideal
    k = max(1, int(smooth / win))
    best_t, best_score = ideal, None
    for i in range(len(energy)):
        t = start + (i + 0.5) * win
        if not lo <= t <= hi:
            continue
        seg = energy[max(0, i - k // 2): i + k // 2 + 1]
        db = 10 * math.log10(sum(seg) / len(seg) + 1)
        score = db + 0.5 * abs(t - ideal)  # 0.5 дБ за секунду от идеала
        if best_score is None or score < best_score:
            best_t, best_score = t, score
    return best_t


def banner_width(W, H, bw_src, bh_src):
    """Ширина баннера, при которой видимая панель занимает TARGET_COVERAGE экрана."""
    pw = (PANEL_X1 - PANEL_X0 + 1) / bw_src
    ph = (PANEL_Y1 - PANEL_Y0 + 1) / bh_src
    aspect = bh_src / bw_src

    def cover(bw):
        return min(bw * pw, W) * (bw * aspect * ph) / (W * H)

    bw = W
    while cover(bw) < TARGET_COVERAGE and bw < 3 * W:
        bw += 2
    return bw + bw % 2, cover(bw)


def build(src, banner, out, preview=False):
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"Не найден {tool}. Установи ffmpeg и добавь его в PATH.")

    v, b = probe(src), probe(banner)
    D, B = v["dur"], b["dur"] / SPEED
    if D > MAX_LEN_ONE_BANNER:
        print(f"ВНИМАНИЕ: ролик {D:.1f} с. Для 2:00-2:59 MusorDrop требует два баннера "
              f"(0:30 и 1:30), этот скрипт ставит только один.")
    if D < B + 2:
        sys.exit(f"Ролик слишком короткий ({D:.1f} с) для баннера {B:.1f} с.")

    # Баннер [t, t+B] в итоговом ролике длиной D+B; середина (D+B)/2 внутри него,
    # точно по центру баннера при t = D/2.
    ideal = D / 2
    slack = max(B / 2 - EDGE_MARGIN, 0)
    t = quietest_point(src, ideal - slack, ideal + slack, ideal) if v["audio"] else ideal
    t = round(t * v["fps"]) / v["fps"]
    t = min(max(t, 1 / v["fps"]), D - 1)

    W, H, fps = v["w"], v["h"], v["fps"]
    if preview:
        k = min(1.0, 540 / min(W, H))  # короткая сторона 540
        W, H = int(W * k) // 2 * 2, int(H * k) // 2 * 2
    bw, cov = banner_width(W, H, b["w"], b["h"])
    if cov < MIN_COVERAGE:
        sys.exit(f"Не удалось добиться {MIN_COVERAGE:.0%} экрана под баннер.")

    gain = 0.0
    li, lb = (loudness(src) if v["audio"] else None), loudness(banner)
    if li is not None and lb is not None:
        gain = li - lb
    gain = max(min(gain, 6.0), -12.0)  # не раздуваем и не глушим баннер до неслышимости

    norm = f"scale={W}:{H}:force_original_aspect_ratio=decrease,pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.6f},format=yuv420p"
    blur = max(8, W // 40)
    f = []
    f.append(f"[0:v]{norm},split=3[va][vb][vc]")
    f.append(f"[va]trim=end={t:.6f},setpts=PTS-STARTPTS[v1]")
    f.append(f"[vb]trim=start={t:.6f},setpts=PTS-STARTPTS,trim=end_frame=1,"
             f"tpad=stop_mode=clone:stop_duration={B + 1:.3f},trim=duration={B:.6f},"
             f"gblur=sigma={blur},eq=brightness=-0.12:saturation=0.8,setpts=PTS-STARTPTS[bg]")
    f.append(f"[1:v]setpts=PTS/{SPEED},fps={fps:.6f},"
             f"chromakey=0x00FF00:0.28:0.06,despill=type=green,"
             f"scale={bw}:-2,format=yuva420p[ban]")
    f.append(f"[bg][ban]overlay=(W-w)/2:(H-h)/2:shortest=0:eof_action=pass,"
             f"trim=duration={B:.6f},setpts=PTS-STARTPTS,format=yuv420p[v2]")
    f.append(f"[vc]trim=start={t:.6f},setpts=PTS-STARTPTS[v3]")

    afmt = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"
    if v["audio"]:
        f.append(f"[0:a]{afmt},asplit=2[aa][ab]")
        f.append(f"[aa]atrim=end={t:.6f},asetpts=PTS-STARTPTS,afade=t=out:st={max(t - 0.03, 0):.6f}:d=0.03[a1]")
        f.append(f"[ab]atrim=start={t:.6f},asetpts=PTS-STARTPTS,afade=t=in:d=0.03[a3]")
    else:
        f.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={t:.6f}[a1]")
        f.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={D - t:.6f}[a3]")
    f.append(f"[1:a]{afmt},atempo={SPEED},volume={gain:.2f}dB,"
             f"apad,atrim=duration={B:.6f},asetpts=PTS-STARTPTS[a2]")
    f.append("[v1][a1][v2][a2][v3][a3]concat=n=3:v=1:a=1[v][a]")

    out.parent.mkdir(parents=True, exist_ok=True)
    enc = ["-c:v", "libx264", "-preset", "veryfast" if preview else "medium",
           "-crf", "23" if preview else "18", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y",
         "-i", str(src), "-i", str(banner), "-filter_complex", ";".join(f),
         "-map", "[v]", "-map", "[a]", *enc, str(out)])

    total = probe(out)["dur"]
    mid = total / 2
    check = out.with_name(out.stem + "_check.png")
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{mid:.3f}", "-i", str(out),
         "-frames:v", "1", str(check)])

    ok = t + EDGE_MARGIN / 2 <= mid <= t + B - EDGE_MARGIN / 2
    print(f"""
Готово: {out}
  исходник        {D:.2f} с, {v['w']}x{v['h']}, {fps:.2f} fps
  рез             {t:.2f} с (самая тихая точка у {ideal:.2f} с)
  баннер          {t:.2f}-{t + B:.2f} с, {B:.2f} с (ускорение {SPEED}x), звук {gain:+.1f} дБ
  площадь баннера {cov:.1%} экрана (нужно от {MIN_COVERAGE:.0%})
  итог            {total:.2f} с, середина {mid:.2f} с -> {'в баннере, OK' if ok else 'ВНЕ баннера, проверь!'}
  кадр середины   {check}
""")
    return ok


def main():
    ap = argparse.ArgumentParser(description="Вставка баннера MusorDrop в середину ролика")
    ap.add_argument("video", type=Path, help="готовый смонтированный ролик")
    ap.add_argument("-o", "--output", type=Path,
                    help="куда сохранить (по умолчанию <имя>_musordrop.mp4 рядом с исходником)")
    ap.add_argument("--banner", type=Path, default=DEFAULT_BANNER,
                    help="файл баннера с зелёным фоном и звуком")
    ap.add_argument("--preview", action="store_true", help="быстрый черновик в 540p")
    a = ap.parse_args()
    if not a.video.is_file():
        sys.exit(f"Нет файла: {a.video}")
    if not a.banner.is_file():
        sys.exit(f"Нет баннера: {a.banner}")
    suffix = "_musordrop_preview.mp4" if a.preview else "_musordrop.mp4"
    out = a.output or a.video.with_name(a.video.stem + suffix)
    sys.exit(0 if build(a.video, a.banner, out, a.preview) else 1)


if __name__ == "__main__":
    main()
