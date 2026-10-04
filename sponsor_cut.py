#!/usr/bin/env python3
"""Спонсорские версии ролика: вставка баннера MusorDrop / FloatUP по rules.json.

Использование:
    python sponsor_cut.py ролик.mp4                      # версия для MusorDrop
    python sponsor_cut.py ролик.mp4 --sponsor floatup
    python sponsor_cut.py ролик.mp4 --sponsor all        # все спонсоры по очереди
    python sponsor_cut.py C:\\...\\aivideos\\niko          # папка проекта: берёт montage\\niko_montage.mp4

Что делает:
  * тайминг по правилам спонсора: MusorDrop - один баннер так, чтобы середина
    итогового ролика попадала в баннер (до 2 мин), дальше 0:30, 1:30...;
    FloatUP - 0:20, 1:20... (ролик короче минуты - середина);
  * рез в самой тихой точке у нужной секунды; если рядом лежит timeline.json
    со словами, предпочитает паузы между словами;
  * основное видео и звук на паузе, фон - размытый затемнённый стоп-кадр,
    баннер по центру (зелёный фон вырезается), с максимальным разрешённым
    ускорением и громкостью 75% от громкости ролика;
  * кодирует на видеокарте (NVIDIA/Intel/AMD), если она есть, иначе libx264;
  * печатает проверки по правилам и сохраняет кадр, куда смотрит проверяющий.

Нужен только ffmpeg/ffprobe в PATH и Python 3.8+.
"""

import argparse
import json
import math
import shutil
import subprocess
import sys
import tempfile
from array import array
from pathlib import Path

HERE = Path(__file__).resolve().parent
RULES = HERE / "rules.json"

# Аппаратные кодеки пробуются по очереди, иначе libx264.
ENCODERS = {
    "h264_nvenc": ["-c:v", "h264_nvenc", "-preset", "p5", "-rc", "vbr", "-cq", "19",
                   "-b:v", "0", "-profile:v", "high"],
    "h264_qsv": ["-c:v", "h264_qsv", "-preset", "medium", "-global_quality", "21"],
    "h264_amf": ["-c:v", "h264_amf", "-quality", "quality", "-rc", "cqp",
                 "-qp_i", "19", "-qp_p", "21"],
}
AFMT = "aresample=48000,aformat=sample_fmts=fltp:channel_layouts=stereo"


def run(cmd, **kw):
    r = subprocess.run(cmd, **kw)
    if r.returncode != 0:
        err = r.stderr if isinstance(r.stderr, str) else ""
        sys.exit(f"Ошибка ffmpeg:\n{' '.join(map(str, cmd[:10]))} ...\n{err[-3000:]}")
    return r


def probe(path):
    out = run(["ffprobe", "-v", "error", "-print_format", "json", "-show_format",
               "-show_streams", str(path)], capture_output=True, text=True).stdout
    info = json.loads(out)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    has_audio = any(s["codec_type"] == "audio" for s in info["streams"])
    num, den = v.get("avg_frame_rate", "0/0").split("/")
    fps = float(num) / float(den) if float(den) else 0.0
    if not 10 <= fps <= 120:
        num, den = v.get("r_frame_rate", "30/1").split("/")
        fps = float(num) / float(den) if float(den) else 30.0
    w, h = int(v["width"]), int(v["height"])
    rot = int(v.get("tags", {}).get("rotate", 0) or 0)
    for sd in v.get("side_data_list", []):
        rot = int(sd.get("rotation", rot) or rot)
    if abs(rot) % 180 == 90:
        w, h = h, w
    dur = float(info["format"]["duration"])
    frames = int(v.get("nb_frames", 0) or 0) or round(dur * fps)
    return {"w": w, "h": h, "fps": fps, "dur": dur, "frames": frames, "audio": has_audio}


def loudness(path, af=None):
    """Интегральная громкость, LUFS (None, если тишина или нет звука)."""
    p = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(path), "-vn",
                        "-af", (af + "," if af else "") + "ebur128", "-f", "null", "-"],
                       capture_output=True, text=True)
    val = None
    for line in p.stderr.split("Summary:")[-1].splitlines():
        line = line.strip()
        if line.startswith("I:"):
            try:
                val = float(line.split()[1])
            except ValueError:
                pass
    return val if val is not None and val > -70 else None


def envelope(path, rate=8000):
    """Громкость звука ролика с шагом 10 мс, дБ, сглажено ±50 мс."""
    pcm = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-vn", "-ac", "1",
                          "-ar", str(rate), "-f", "s16le", "-"], capture_output=True).stdout
    x = array("h")
    x.frombytes(pcm[: len(pcm) // 2 * 2])
    if sys.byteorder == "big":
        x.byteswap()
    hop = rate // 100
    ms = [sum(s * s for s in x[i:i + hop]) / hop for i in range(0, len(x) - hop + 1, hop)]
    out = []
    for i in range(len(ms)):
        seg = ms[max(0, i - 5): i + 6]
        out.append(10 * math.log10(sum(seg) / len(seg) / 32768 ** 2 + 1e-12))
    return out


def word_gaps(video):
    """Паузы между словами из timeline.json монтажа (если есть)."""
    for p in (video.parent / "timeline.json", video.parent / "montage" / "timeline.json"):
        if p.is_file():
            try:
                words = [w for w in json.loads(p.read_text(encoding="utf-8")).get("words", [])
                         if "start" in w and "end" in w]
            except (ValueError, AttributeError):
                return []
            return [(a["end"], b["start"]) for a, b in zip(words, words[1:])]
    return []


def pick_cut(target, tol, env, gaps, dur, fps):
    """Кадр реза у target: тише всего, в паузе между словами, ближе к цели."""
    lo, hi = max(1.0, target - tol), min(dur - 1.0, target + tol)
    best = None
    for i in range(int(lo * 100), min(int(hi * 100) + 1, len(env))):
        t = i / 100
        s = env[i] + 4.0 * abs(t - target) / max(tol, 0.1)
        if any(g0 - 0.03 <= t <= g1 + 0.03 for g0, g1 in gaps):
            s -= 6.0
        if best is None or s < best[0]:
            best = (s, t)
    t = best[1] if best else target
    return max(1, min(round(t * fps), round(dur * fps) - 1))


def plan_targets(sp, C, B):
    """Секунды исходного ролика, где ставить баннеры, и какое правило применено."""
    if sp["timing"] == "floatup":
        if C + B < 60:
            return [C / 2], "середина (ролик короче минуты)"
        ts, k = [], 0
        while 20 + 60 * k - k * B < C - 1.0:
            ts.append(20 + 60 * k - k * B)
            k += 1
        return ts, "на 0:20, 1:20, 2:20... итогового ролика"
    if sp["timing"] == "musordrop":
        if C + B < 120:
            return [C / 2], "один баннер, середина ролика внутри баннера (ролик до 2 мин)"
        n = 2
        while math.floor((C + n * B) / 60) > n:
            n += 1
        return [30 + 60 * k - k * B for k in range(n)], "середина каждой минуты (0:30, 1:30...)"
    raise ValueError(sp["timing"])


def banner_size(sp, b, W, H):
    """Ширина баннера на экране и доля экрана, которую занимает видимая панель."""
    px, py, pw, ph = sp["panel"] or [0, 0, b["w"], b["h"]]

    def area(bw):
        s = bw / b["w"]
        return min(pw * s, W) * ph * s / (W * H)

    bw = W
    if sp["crop"]:
        while area(bw) < sp["targetArea"] and bw < 3 * W:
            bw += 2
    return bw + bw % 2, area(bw)


def pick_encoder(force_cpu, preview):
    if not force_cpu:
        for name, args in ENCODERS.items():
            test = subprocess.run(
                ["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=s=1280x720:d=0.2",
                 "-frames:v", "3", *args, "-f", "null", "-"], capture_output=True)
            if test.returncode == 0:
                return name, args
    return "libx264", ["-c:v", "libx264", "-preset", "veryfast",
                       "-crf", "23" if preview else "18"]


def build(src, key, sp, out, enc, v, W, H, env, gaps, LV):
    banner = (HERE / sp["banner"]).resolve()
    if not banner.is_file():
        sys.exit(f"Нет баннера {sp['title']}: {banner}")
    b = probe(banner)
    fps, NF = v["fps"], v["frames"]
    C = NF / fps
    nfr = math.floor(b["dur"] / sp["speed"] * fps)
    B = nfr / fps

    targets, why = plan_targets(sp, C, B)
    tol = sp["tolerance"]
    if len(targets) == 1 and sp["timing"] == "musordrop":
        tol = min(tol, B / 2 - 0.75)  # середина итогового ролика с запасом внутри баннера
    cuts = sorted({pick_cut(t, tol, env, gaps, C, fps) for t in targets})
    n = len(cuts)

    bw, area = banner_size(sp, b, W, H)
    gain = 20 * math.log10(sp["volume"])
    LB = loudness(banner, f"atempo={sp['speed']}")
    if LB is not None:
        gain += (LV if LV is not None else -14.0) - LB

    norm = (f"scale={W}:{H}:force_original_aspect_ratio=decrease,"
            f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps:.6f},format=yuv420p")
    bounds = [0] + cuts + [NF]
    ins, f, order = [], [], ""
    with tempfile.TemporaryDirectory() as tmp:
        # Входы 0..n - куски ролика между резами, каждый читается отдельно.
        for i in range(n + 1):
            f0, f1 = bounds[i], bounds[i + 1]
            # Границы по середине между кадрами: кадр f0 входит, кадр f1 уже нет.
            t0 = max(f0 - 0.5, 0) / fps
            seek = ["-ss", f"{t0:.6f}"] if f0 else []
            ins += [*seek, "-t", f"{(f1 - 0.5) / fps - t0:.6f}", "-i", str(src)]
        # Входы n+1..2n - стоп-кадры (последний кадр перед резом).
        for j, k in enumerate(cuts):
            still = Path(tmp) / f"still{j}.png"
            run(["ffmpeg", "-v", "error", "-y", "-ss", f"{max(k - 1.5, 0) / fps:.6f}",
                 "-i", str(src), "-frames:v", "1", str(still)], capture_output=True, text=True)
            ins += ["-loop", "1", "-framerate", f"{fps:.6f}", "-t", f"{B + 1:.3f}", "-i", str(still)]
        bi = 2 * n + 1
        ins += ["-i", str(banner)]

        key_f = f"format=rgba,colorkey={sp['key']}:0.30:0.08," if sp["key"] else ""
        f.append(f"[{bi}:v]setpts=(PTS-STARTPTS)/{sp['speed']},fps={fps:.6f},{key_f}"
                 f"scale={bw}:-2:flags=lanczos,trim=end_frame={nfr},split={n}"
                 + "".join(f"[bn{j}]" for j in range(n)))
        f.append(f"[{bi}:a]{AFMT},atempo={sp['speed']},volume={gain:.2f}dB,"
                 f"alimiter=limit=0.89:level=false,apad,atrim=duration={B:.6f},"
                 f"afade=t=in:d=0.01,afade=t=out:st={B - 0.04:.4f}:d=0.04,asplit={n}"
                 + "".join(f"[ba{j}]" for j in range(n)))
        blur = max(8, W // 40)
        for i in range(n + 1):
            d = (bounds[i + 1] - bounds[i]) / fps
            f.append(f"[{i}:v]{norm},trim=end_frame={bounds[i + 1] - bounds[i]},setpts=PTS-STARTPTS[v{i}]")
            if v["audio"]:
                f.append(f"[{i}:a]{AFMT},asetpts=PTS-STARTPTS,afade=t=in:d=0.02,"
                         f"afade=t=out:st={max(d - 0.03, 0):.4f}:d=0.03[a{i}]")
            else:
                f.append(f"anullsrc=r=48000:cl=stereo,atrim=duration={d:.6f}[a{i}]")
            order += f"[v{i}][a{i}]"
            if i < n:
                f.append(f"[{n + 1 + i}:v]{norm},gblur=sigma={blur},eq=brightness=-0.12:saturation=0.8,"
                         f"trim=end_frame={nfr},setpts=PTS-STARTPTS[bg{i}]")
                f.append(f"[bg{i}][bn{i}]overlay=(W-w)/2:(H-h)/2:format=auto:eof_action=repeat,"
                         f"trim=end_frame={nfr},setpts=PTS-STARTPTS,format=yuv420p[vb{i}]")
                order += f"[vb{i}][ba{i}]"
        f.append(f"{order}concat=n={2 * n + 1}:v=1:a=1[v][a]")

        print(f"\n{sp['title']}: {why}; баннер {B:.2f} с x{n}, рез на "
              f"{', '.join(f'{k / fps:.2f}' for k in cuts)} с", flush=True)
        run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-stats", "-y", *ins,
             "-filter_complex", ";".join(f), "-map", "[v]", "-map", "[a]",
             *enc, "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
             "-movflags", "+faststart", str(out)])

    # Проверки по правилам спонсора.
    R = probe(out)
    T = R["dur"]
    fin = [(k / fps + i * B, k / fps + (i + 1) * B) for i, k in enumerate(cuts)]
    checks = [(f"длина {T:.2f} с (ожидалось {C + n * B:.2f} с), кадров {R['frames']} из {NF + n * nfr}",
               abs(T - (C + n * B)) < 0.15 and abs(R["frames"] - (NF + n * nfr)) <= 1)]
    if sp["timing"] == "floatup" and C + B >= 60:
        for k, (s, _) in enumerate(fin):
            checks.append((f"баннер {k + 1} стартует на {s:.2f} с (нужно {20 + 60 * k} с ±{tol})",
                           abs(s - (20 + 60 * k)) <= tol + 1e-6))
        chk = fin[0][0] + 1.0
    elif sp["timing"] == "musordrop" and n > 1:
        for k, (s, _) in enumerate(fin):
            checks.append((f"баннер {k + 1} на {s:.2f} с (нужно ~{30 + 60 * k} с)",
                           abs(s - (30 + 60 * k)) <= 2.5))
        chk = fin[0][0] + 1.0
    else:
        chk = T / 2
        checks.append((f"середина ролика {chk:.2f} с внутри баннера {fin[0][0]:.2f}-{fin[0][1]:.2f} с",
                       any(s + 0.4 <= chk <= e - 0.4 for s, e in fin)))
    checks.append((f"размер {area:.1%} экрана (нужно от {sp['minArea']:.0%})", area >= sp["minArea"]))
    LI = loudness(out, f"atrim=start={fin[0][0] + 0.3:.2f}:end={fin[0][1] - 0.2:.2f}")
    if LI is not None and LV is not None:
        checks.append((f"громкость баннера {LI:.1f} LUFS, ролика {LV:.1f} LUFS "
                       f"(цель {20 * math.log10(sp['volume']):+.1f} дБ)", LI > LV - 6))
    else:
        checks.append(("звук баннера есть" if LI is not None else "звук баннера НЕ найден", LI is not None))
    checks.append((f"скорость баннера {sp['speed']}x (лимит спонсора)", True))

    check_png = out.with_name(out.stem + "_check.png")
    run(["ffmpeg", "-v", "error", "-y", "-ss", f"{chk:.3f}", "-i", str(out),
         "-frames:v", "1", str(check_png)], capture_output=True, text=True)

    lines = [f"{sp['title']}: {out.name}, {T:.2f} с - {why}"]
    lines += [f"  баннер {s:.2f}-{e:.2f} с, скорость {sp['speed']}x, звук {gain:+.1f} дБ" for s, e in fin]
    lines += [("  OK   " if ok else "  ОШИБКА ") + text for text, ok in checks]
    lines.append(f"  кадр проверки: {check_png.name}")
    print("\n".join(lines), flush=True)
    return all(ok for _, ok in checks), lines


def find_source(path):
    if path.is_file():
        return path
    if path.is_dir():
        mdir = path / "montage" if (path / "montage").is_dir() else path
        exact = mdir / f"{path.name}_montage.mp4"
        if exact.is_file():
            return exact
        cands = sorted(mdir.glob("*_montage.mp4"), key=lambda p: p.stat().st_mtime) or \
            sorted((p for p in mdir.glob("*.mp4") if "_" not in p.stem[-12:]), key=lambda p: p.stat().st_mtime)
        if cands:
            return cands[-1]
    sys.exit(f"Не нашёл ролик: {path}")


def main():
    ap = argparse.ArgumentParser(description="Спонсорские версии ролика (MusorDrop / FloatUP)")
    ap.add_argument("video", type=Path, help="готовый ролик или папка проекта")
    ap.add_argument("--sponsor", default="musordrop",
                    help="musordrop (по умолчанию), floatup, all или список через запятую")
    ap.add_argument("--out-dir", type=Path, help="куда сохранять (по умолчанию рядом с роликом)")
    ap.add_argument("--hd", action="store_true",
                    help="выход 1080x1920 вместо 4K (TikTok всё равно показывает 1080p), в разы быстрее")
    ap.add_argument("--preview", action="store_true", help="быстрый черновик, короткая сторона 540")
    ap.add_argument("--cpu", action="store_true", help="не использовать видеокарту, кодировать libx264")
    ap.add_argument("--banner-volume", type=float,
                    help="громкость баннера относительно ролика (по умолчанию из rules.json, 0.75)")
    a = ap.parse_args()

    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            sys.exit(f"Не найден {tool}. Установи ffmpeg и добавь его в PATH.")
    rules = {k: s for k, s in json.loads(RULES.read_text(encoding="utf-8")).items()
             if not k.startswith("_")}
    keys = list(rules) if a.sponsor == "all" else [s.strip() for s in a.sponsor.split(",") if s.strip()]
    for k in keys:
        if k not in rules:
            sys.exit(f"Нет спонсора {k} в rules.json (есть: {', '.join(rules)})")

    src = find_source(a.video)
    v = probe(src)
    W, H = v["w"], v["h"]
    short = 540 if a.preview else 1080 if a.hd else None
    if short:
        s = min(1.0, short / min(W, H))
        W, H = int(W * s) // 2 * 2, int(H * s) // 2 * 2
    enc_name, enc = pick_encoder(a.cpu, a.preview)
    print(f"Ролик: {src}\n  {v['dur']:.2f} с, {v['w']}x{v['h']}, {v['fps']:.2f} fps -> выход {W}x{H}, кодек {enc_name}")

    env = envelope(src) if v["audio"] else [-120.0] * int(v["dur"] * 100 + 1)
    gaps = word_gaps(src)
    LV = loudness(src) if v["audio"] else None
    out_dir = a.out_dir or src.parent
    out_dir.mkdir(parents=True, exist_ok=True)
    suffix = "_preview" if a.preview else ""

    all_ok, report = True, []
    for k in keys:
        sp = dict(rules[k])
        if a.banner_volume:
            sp["volume"] = a.banner_volume
        out = out_dir / f"{src.stem}_{k}{suffix}.mp4"
        ok, lines = build(src, k, sp, out, enc, v, W, H, env, gaps, LV)
        all_ok &= ok
        report += lines + [""]
    (out_dir / f"{src.stem}_sponsors_report.txt").write_text("\n".join(report), encoding="utf-8")
    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
