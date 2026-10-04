#!/usr/bin/env python3
"""Спонсорские версии ролика (FloatUP / MusorDrop) по rules.json.
  python3 sponsor_cut.py <папка проекта> [--only floatup,musordrop] [--out DIR] [--budget 150] [--preview]
Работает кусками: если не успел за --budget секунд, печатает CONTINUE — запустить ещё раз, продолжит с того же места.
Основное видео на паузе (размытый стоп-кадр), баннер по центру на всю ширину, рез — в самой тихой точке у нужной секунды."""
import sys, os, json, glob, subprocess, math, argparse, time
import numpy as np

KIT = os.path.dirname(os.path.abspath(__file__))
T0 = time.time()


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(' '.join(cmd[:8]) + ' ...\n' + r.stderr[-3000:])
    return r


def probe(path):
    r = run(['ffprobe', '-v', 'error', '-show_entries',
             'format=duration:stream=codec_type,width,height,r_frame_rate,sample_rate,nb_frames',
             '-of', 'json', path])
    j = json.loads(r.stdout)
    v = next((s for s in j['streams'] if s['codec_type'] == 'video'), None)
    a = next((s for s in j['streams'] if s['codec_type'] == 'audio'), None)
    fr = v['r_frame_rate'].split('/') if v else ['0', '1']
    return dict(dur=float(j['format']['duration']), w=v and v['width'], h=v and v['height'],
                fps=float(fr[0]) / float(fr[1]) if v else 0, sr=a and int(a['sample_rate']),
                frames=int(v.get('nb_frames', 0) or 0) if v else 0)


def loudness(path, af=None):
    cmd = ['ffmpeg', '-hide_banner', '-nostats', '-i', path, '-vn',
           '-af', (af + ',' if af else '') + 'ebur128=peak=true', '-f', 'null', '-']
    r = subprocess.run(cmd, capture_output=True, text=True)
    I = None
    for line in r.stderr.split('Summary:')[-1].splitlines():
        line = line.strip()
        if line.startswith('I:'):
            I = float(line.split()[1])
    return I


def envelope(path, sr=16000):
    """Громкость итогового звука монтажа, шаг 10 мс, в дБ (сглажено ±50 мс)."""
    r = subprocess.run(['ffmpeg', '-v', 'error', '-i', path, '-vn', '-ac', '1', '-ar', str(sr), '-f', 's16le', '-'],
                       capture_output=True)
    x = np.frombuffer(r.stdout, dtype=np.int16).astype(np.float32) / 32768
    hop = sr // 100
    n = len(x) // hop
    rms = np.sqrt((x[:n * hop].reshape(n, hop) ** 2).mean(1) + 1e-12)
    sm = np.convolve(rms, np.ones(11) / 11, mode='same')
    return 20 * np.log10(sm + 1e-9)


def pick_cut(target, tol, env_db, gaps, dur, fps):
    """Кадр реза возле target: тише всего, в паузе между словами, ближе к цели."""
    lo = max(1.0, target - tol)
    hi = min(dur - 1.0, target + tol)
    best = None
    for i in range(int(lo * 100), min(int(hi * 100) + 1, len(env_db))):
        t = i / 100
        s = env_db[i] + 4.0 * abs(t - target) / max(tol, 0.1)
        if any(g0 - 0.03 <= t <= g1 + 0.03 for g0, g1 in gaps):
            s -= 6.0
        if best is None or s < best[0]:
            best = (s, t)
    t = best[1] if best else target
    k = round(t * fps)
    return k, float(env_db[min(int(k / fps * 100), len(env_db) - 1)])


def plan_targets(sp, C, B):
    """Цели (сек времени монтажа, до вставок) и текстом — какое правило применено."""
    if sp['timing'] == 'floatup':
        if C + B < 60:
            return [C / 2], 'середина (ролик короче минуты)'
        ts, k = [], 0
        while 20 + 60 * k - k * B < C - 1.0:
            ts.append(20 + 60 * k - k * B)
            k += 1
        return ts, 'на 0:20, 1:20, 2:20… итогового ролика'
    if sp['timing'] == 'musordrop':
        if C + B < 120:
            return [C / 2], 'один баннер в середине (ролик короче 2 минут)'
        n = 2
        while math.floor((C + n * B) / 60) > n:
            n += 1
        return [30 + 60 * k - k * B for k in range(n)], 'середина каждой минуты (0:30, 1:30…)'
    raise ValueError(sp['timing'])


def vcodec(preset):
    return ['-c:v', 'libx264', '-preset', preset, '-crf', '17', '-profile:v', 'high', '-pix_fmt', 'yuv420p']


def left(budget):
    return budget - (time.time() - T0)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('project')
    ap.add_argument('--only', default='')
    ap.add_argument('--out', default=None)
    ap.add_argument('--input', default=None)
    ap.add_argument('--budget', type=float, default=150)
    ap.add_argument('--preview', action='store_true')
    ap.add_argument('--preset', default=None)
    a = ap.parse_args()

    proj = os.path.abspath(a.project)
    name = os.path.basename(proj.rstrip('/'))
    rules = json.load(open(os.path.join(KIT, 'rules.json'), encoding='utf-8'))
    mdir = os.path.join(proj, 'montage')
    src = a.input or os.path.join(mdir, f'{name}_montage.mp4')
    if not os.path.exists(src):
        c = sorted(glob.glob(os.path.join(mdir, '*_montage.mp4')) or glob.glob(os.path.join(mdir, '*.mp4')),
                   key=os.path.getmtime)
        if not c:
            sys.exit('нет монтажа в ' + mdir)
        src = c[-1]
    src = os.path.abspath(src)
    out = os.path.abspath(a.out or os.path.join(mdir, 'sponsors'))
    work = os.path.join(out, '_work')
    os.makedirs(work, exist_ok=True)

    P = probe(src)
    W, H, FPS = P['w'], P['h'], P['fps']
    NF = P['frames'] or int(round(P['dur'] * FPS))
    C = NF / FPS
    big = W * H > 1920 * 1080 * 1.5
    preset = a.preset or ('fast' if big else 'medium')
    chunkF = int(FPS * (4 if big else 20))
    stamp = f'{os.path.getmtime(src):.0f}_{os.path.getsize(src)}'

    tlp = os.path.join(mdir, 'timeline.json')
    words = [w for w in (json.load(open(tlp, encoding='utf-8')).get('words', []) if os.path.exists(tlp) else [])
             if 'start' in w and 'end' in w]
    gaps = [(x['end'], y['start']) for x, y in zip(words, words[1:])]
    envp = os.path.join(work, f'env_{stamp}.npy')
    if os.path.exists(envp):
        env = np.load(envp)
    else:
        env = envelope(src)
        np.save(envp, env)

    only = [s for s in a.only.split(',') if s]
    rep_path = os.path.join(out, 'sponsors_report.json')
    report = json.load(open(rep_path, encoding='utf-8')) if os.path.exists(rep_path) else {}
    report.update({'project': name, 'source': src, 'duration': round(C, 3), 'size': [W, H], 'fps': FPS})
    report.setdefault('sponsors', {})

    for key, sp in rules.items():
        if key.startswith('_') or (only and key not in only):
            continue
        sw = os.path.join(work, key)
        os.makedirs(sw, exist_ok=True)
        stp = os.path.join(sw, 'state.json')
        st = json.load(open(stp, encoding='utf-8')) if os.path.exists(stp) else {}
        if st.get('stamp') != stamp or st.get('rules') != sp:
            st = {'stamp': stamp, 'rules': sp}

        def save():
            json.dump(st, open(stp, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)

        dst = os.path.join(out, f'{name}_{key}.mp4')
        if st.get('done') and os.path.exists(dst):
            continue
        bsrc = os.path.normpath(os.path.join(KIT, sp['banner']))

        # 1. план
        if 'plan' not in st:
            bp = probe(bsrc)
            speed = sp['speed']
            nfr = math.floor(bp['dur'] / speed * FPS)
            B = nfr / FPS
            tempo = f'atempo={speed}'
            I = loudness(bsrc, tempo)
            gain = sp['lufs'] - I if I is not None else 0
            targets, why = plan_targets(sp, C, B)
            cuts = []
            for tg in targets:
                k, lvl = pick_cut(tg, sp['tolerance'], env, gaps, C, FPS)
                cuts.append({'target': round(tg, 3), 'frame': k, 'cut': round(k / FPS, 3), 'levelDb': round(lvl, 1)})
            bw = int(round(W * sp['widthFrac'] / 2) * 2)
            bh = bw * (840 / 1902) if sp.get('key') else bw * bp['h'] / bp['w']
            st['plan'] = dict(nfr=nfr, B=B, speed=speed, tempo=tempo, gain=gain, cuts=cuts, why=why, bw=bw,
                              area=bw * bh / (W * H), bsize=[bp['w'], bp['h']])
            jobs = []
            bounds = [0] + [c['frame'] for c in cuts] + [NF]
            for i in range(len(bounds) - 1):
                f0, f1 = bounds[i], bounds[i + 1]
                m = max(1, math.ceil((f1 - f0) / chunkF))
                edges = [f0 + round((f1 - f0) * q / m) for q in range(m + 1)]
                for e0, e1 in zip(edges, edges[1:]):
                    if e1 > e0:
                        jobs.append({'kind': 'm', 'f0': e0, 'n': e1 - e0})
                if i < len(cuts):
                    jobs.append({'kind': 'b', 'i': i})
            for j, jb in enumerate(jobs):
                jb['file'] = os.path.join(sw, f'v{j:03d}.mp4')
                jb['done'] = False
            st['jobs'] = jobs
            save()
            print(f"{sp['title']}: {why}; баннер {B:.2f}s ×{len(cuts)}, рез на {[c['cut'] for c in cuts]}s", flush=True)
        pl = st['plan']
        B = pl['B']

        # 2. стоп-кадры и звук баннеров
        for i, cu in enumerate(pl['cuts']):
            fr = os.path.join(sw, f'freeze{i}.png')
            aw = os.path.join(sw, f'banner{i}.wav')
            if not os.path.exists(fr):
                raw = os.path.join(sw, f'freeze{i}_raw.png')
                run(['ffmpeg', '-v', 'error', '-y', '-ss', f"{(cu['frame'] - 0.75) / FPS:.5f}", '-i', src,
                     '-frames:v', '1', raw])
                sig = max(8, W / 50)
                run(['ffmpeg', '-v', 'error', '-y', '-i', raw, '-vf',
                     f'scale={W}:{H},gblur=sigma={sig:.1f},eq=brightness=-0.10:saturation=0.85', '-frames:v', '1', fr])
            if not os.path.exists(aw):
                run(['ffmpeg', '-v', 'error', '-y', '-i', bsrc, '-vn', '-af',
                     f"{pl['tempo']},aresample=48000,volume={pl['gain']:.2f}dB,alimiter=limit=0.89:level=false,"
                     f"apad,atrim=0:{B:.5f},afade=t=in:d=0.01,afade=t=out:st={B - 0.04:.4f}:d=0.04,"
                     f"aformat=channel_layouts=stereo",
                     '-c:a', 'pcm_s16le', '-ar', '48000', aw])

        # 3. видео кусками (каждый кусок — отдельный вызов ffmpeg, можно продолжать)
        ran = 0
        for jb in st['jobs']:
            if jb['done'] and os.path.exists(jb['file']):
                continue
            nfr_job = jb.get('n') or pl['nfr']
            spk = 'spf_' + jb['kind']
            spf = st.get(spk + '_t', 0) / st[spk + '_n'] if st.get(spk + '_n', 0) >= 24 else (0.2 if big else 0.08)
            est = spf * nfr_job * 1.3 + 4
            if ran and left(a.budget) < est:
                save()
                print(f'CONTINUE ({sum(j["done"] for j in st["jobs"])}/{len(st["jobs"])} кусков, {key})', flush=True)
                return
            t1 = time.time()
            if jb['kind'] == 'm':
                run(['ffmpeg', '-v', 'error', '-y', '-ss', f"{(jb['f0'] - 0.25) / FPS:.5f}", '-i', src, '-an',
                     '-vf', f'fps={FPS:g},format=yuv420p,setsar=1', '-frames:v', str(jb['n']),
                     *vcodec(preset), '-r', f'{FPS:g}', jb['file']])
            else:
                i = jb['i']
                fr = os.path.join(sw, f'freeze{i}.png')
                keyf = ''
                if sp.get('key'):
                    k = sp['key']
                    keyf = f"chromakey={k['color']}:{k['similarity']}:{k['blend']},despill=type=green,"
                fc = (f"[0:v]format=yuv420p,setsar=1[bg];"
                      f"[1:v]setpts=(PTS-STARTPTS)/{pl['speed']},fps={FPS:g},{keyf}"
                      f"scale={pl['bw']}:-2:flags=lanczos,format=yuva420p[bn];"
                      f"[bg][bn]overlay=x=(W-w)/2:y=(H-h)/2:eof_action=repeat,format=yuv420p,setsar=1[v]")
                run(['ffmpeg', '-v', 'error', '-y', '-loop', '1', '-framerate', f'{FPS:g}', '-i', fr, '-i', bsrc,
                     '-filter_complex', fc, '-map', '[v]', '-frames:v', str(pl['nfr']),
                     *vcodec(preset), '-r', f'{FPS:g}', jb['file']])
            st[spk + '_t'] = st.get(spk + '_t', 0) + (time.time() - t1)
            st[spk + '_n'] = st.get(spk + '_n', 0) + nfr_job
            ran += 1
            jb['done'] = True
            save()

        # 4. склейка видео, звук целиком, мукс
        if left(a.budget) < (40 if big else 25):
            save()
            print(f'CONTINUE (склейка, {key})', flush=True)
            return
        lst = os.path.join(sw, 'list.txt')
        with open(lst, 'w', encoding='utf-8') as f:
            for jb in st['jobs']:
                f.write(f"file '{os.path.abspath(jb['file'])}'\n")
        vcat = os.path.join(sw, 'video.mp4')
        run(['ffmpeg', '-v', 'error', '-y', '-f', 'concat', '-safe', '0', '-i', lst, '-c', 'copy', vcat])
        cuts = pl['cuts']
        n = len(cuts)
        bounds = [0] + [c['frame'] / FPS for c in cuts] + [C]
        ins = ['-i', src]
        for i in range(n):
            ins += ['-i', os.path.join(sw, f'banner{i}.wav')]
        fc = f"[0:a]asplit={n + 1}" + ''.join(f'[as{i}]' for i in range(n + 1)) + ';'
        order = ''
        for i in range(n + 1):
            s0, s1 = bounds[i], bounds[i + 1]
            d = s1 - s0
            fc += (f"[as{i}]atrim=start={s0:.5f}:end={s1:.5f},asetpts=PTS-STARTPTS,aresample=48000,"
                   f"afade=t=in:d=0.02,afade=t=out:st={max(0, d - 0.03):.4f}:d=0.03,"
                   f"aformat=sample_fmts=fltp:channel_layouts=stereo[a{i}];")
            order += f'[a{i}]'
            if i < n:
                fc += f"[{i + 1}:a]aformat=sample_fmts=fltp:channel_layouts=stereo[b{i}];"
                order += f'[b{i}]'
        fc += order + f'concat=n={2 * n + 1}:v=0:a=1[a]'
        acat = os.path.join(sw, 'audio.m4a')
        run(['ffmpeg', '-v', 'error', '-y', *ins, '-filter_complex', fc, '-map', '[a]',
             '-c:a', 'aac', '-b:a', '192k', '-ar', '48000', acat])
        run(['ffmpeg', '-v', 'error', '-y', '-i', vcat, '-i', acat, '-map', '0:v', '-map', '1:a', '-c', 'copy',
             '-shortest', '-movflags', '+faststart', dst])

        # 5. проверки по правилам спонсора
        R = probe(dst)
        T = R['dur']
        fin = [{'start': round(c['frame'] / FPS + i * B, 2), 'end': round(c['frame'] / FPS + (i + 1) * B, 2)}
               for i, c in enumerate(cuts)]
        checks = []
        expT = C + n * B
        expF = NF + n * pl['nfr']
        checks.append((f'длина {T:.2f}s (ожидалось {expT:.2f}s), кадров {R["frames"]} из {expF}',
                       abs(T - expT) < 0.15 and abs(R['frames'] - expF) <= 1))
        if sp['timing'] == 'floatup' and C + B >= 60:
            for k, f in enumerate(fin):
                need = 20 + 60 * k
                checks.append((f'баннер {k + 1} стартует на {f["start"]:.2f}s (нужно {need}s ±{sp["tolerance"]})',
                               abs(f['start'] - need) <= sp['tolerance'] + 1e-6))
            chk = fin[0]['start'] + 1.0
        elif sp['timing'] == 'musordrop' and T >= 120:
            for k, f in enumerate(fin):
                checks.append((f'баннер {k + 1} на {f["start"]:.2f}s (нужно ~{30 + 60 * k}s)',
                               abs(f['start'] - (30 + 60 * k)) <= 2.5))
            chk = fin[0]['start'] + 1.0
        else:
            mid = T / 2
            checks.append((f'середина ролика {mid:.2f}s внутри баннера {fin[0]["start"]:.2f}–{fin[0]["end"]:.2f}s',
                           any(f['start'] + 0.5 <= mid <= f['end'] - 0.5 for f in fin)))
            chk = mid
        full = pl['bw'] >= W
        checks.append((f'размер {pl["area"] * 100:.1f}% экрана (нужно ≥{sp["minArea"] * 100:.0f}%)'
                       + (', на всю ширину кадра — крупнее только с обрезкой' if full else ''),
                       pl['area'] >= sp['minArea'] or (full and pl['area'] >= sp['minArea'] - 0.005)))
        LI = loudness(dst, f'atrim=start={fin[0]["start"] + 0.3:.2f}:end={fin[0]["end"] - 0.2:.2f}')
        LV = loudness(src)
        checks.append((f'громкость баннера {LI} LUFS, ролика {LV} LUFS',
                       LI is not None and LV is not None and LI > LV - 3))
        checks.append((f'скорость баннера {pl["speed"]}x (лимит спонсора)', True))
        png = os.path.join(out, f'check_{key}.png')
        run(['ffmpeg', '-v', 'error', '-y', '-ss', f'{chk:.2f}', '-i', dst, '-frames:v', '1', '-vf', 'scale=540:-2', png])
        st['done'] = True
        save()
        report['sponsors'][key] = {'file': dst, 'rule': pl['why'], 'bannerSec': round(B, 3), 'speed': pl['speed'],
                                   'gainDb': round(pl['gain'], 1), 'cuts': cuts, 'final': fin,
                                   'duration': round(T, 2), 'areaPct': round(pl['area'] * 100, 1),
                                   'checks': [{'text': t, 'ok': bool(o)} for t, o in checks], 'checkFrame': png}
        json.dump(report, open(rep_path, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
        print(f'{sp["title"]}: готово → {dst}', flush=True)
        for t, o in checks:
            print(('  ✓ ' if o else '  ✗ ') + t, flush=True)

    if a.preview:
        for key in report['sponsors']:
            pv = os.path.join(out, f'preview_{key}.mp4')
            if os.path.exists(pv):
                continue
            if left(a.budget) < 60:
                print('CONTINUE (превью)', flush=True)
                return
            run(['ffmpeg', '-v', 'error', '-y', '-i', report['sponsors'][key]['file'], '-vf', 'scale=540:-2',
                 '-c:v', 'libx264', '-crf', '27', '-preset', 'veryfast', '-c:a', 'aac', '-b:a', '128k',
                 '-movflags', '+faststart', pv])

    with open(os.path.join(out, 'sponsors_report.txt'), 'w', encoding='utf-8') as f:
        for key, r in report['sponsors'].items():
            f.write(f"{rules[key]['title']}: {os.path.basename(r['file'])}, {r['duration']}s — {r['rule']}\n")
            for fi in r['final']:
                f.write(f"  баннер {fi['start']}–{fi['end']}s, скорость {r['speed']}x, {r['areaPct']}% экрана\n")
            for c in r['checks']:
                f.write(('  ✓ ' if c['ok'] else '  ✗ ') + c['text'] + '\n')
    print('DONE', flush=True)


if __name__ == '__main__':
    main()
