#!/usr/bin/env python3
"""
recording_review.py - founder screen-recording review helper.

WHY THIS EXISTS
Fable can see a recording (ffmpeg -> frames -> image review) but could NOT hear it.
Any spoken comment on a founder clip was silently lost. This closes that gap with an
OFFLINE recogniser whose acoustic model ships inside the pip wheel, because the
container can reach pypi but NOT huggingface / openaipublic / alphacephei (all 403).

SETUP (once per container):
    pip install pocketsphinx --break-system-packages

USAGE:
    python3 recording_review.py <video> [--frames-every 1.0] [--outdir /home/claude/rec]

OUTPUT:
    <outdir>/frames/f_%04d.png   frames for visual review
    <outdir>/audio.wav           16k mono PCM
    <outdir>/transcript.txt      timestamped best-effort transcript

HONEST LIMITS - state these to the founder every time, never bury them:
  * pocketsphinx is a small offline model. On phone audio with an Indian accent expect
    PARTIAL words, not a clean transcript. It is a pointer to WHERE you spoke and roughly
    WHAT about - not a quotation. Never treat its output as the founder's exact words and
    never act on a card built only from it.
  * If a segment matters, ask the founder to confirm it in text.
  * Silence or music decodes to '' - that is correct behaviour, not a failure.
"""
import argparse, os, subprocess, sys, wave


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def extract(video, outdir, frames_every):
    os.makedirs(f"{outdir}/frames", exist_ok=True)
    run(["ffmpeg", "-y", "-i", video, "-vf", f"fps=1/{frames_every}",
         f"{outdir}/frames/f_%04d.png"])
    run(["ffmpeg", "-y", "-i", video, "-vn", "-ac", "1", "-ar", "16000",
         "-acodec", "pcm_s16le", f"{outdir}/audio.wav"])
    n = len(os.listdir(f"{outdir}/frames"))
    return n


def has_audio(wav):
    if not os.path.exists(wav):
        return False
    with wave.open(wav, "rb") as w:
        return w.getnframes() > 0


def transcribe(wav, chunk_s=8.0):
    """Decode in chunks so every line carries a timestamp - the founder says 'at 0:11'
    and we need to land on that frame, not search a wall of text."""
    from pocketsphinx import Decoder, Config
    mp = __import__("pocketsphinx").get_model_path()
    c = Config()
    c.set_string("-hmm", os.path.join(mp, "en-us", "en-us"))
    c.set_string("-lm", os.path.join(mp, "en-us", "en-us.lm.bin"))
    c.set_string("-dict", os.path.join(mp, "en-us", "cmudict-en-us.dict"))
    c.set_string("-logfn", "/dev/null")
    d = Decoder(c)

    with wave.open(wav, "rb") as w:
        sr, n = w.getframerate(), w.getnframes()
        step = int(sr * chunk_s)
        out = []
        for start in range(0, n, step):
            w.setpos(start)
            buf = w.readframes(min(step, n - start))
            d.start_utt()
            d.process_raw(buf, False, True)
            d.end_utt()
            hyp = d.hyp()
            text = hyp.hypstr.strip() if hyp else ""
            if text:
                t = start / sr
                out.append(f"[{int(t//60)}:{int(t%60):02d}] {text}")
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("video")
    ap.add_argument("--frames-every", type=float, default=1.0)
    ap.add_argument("--outdir", default="/home/claude/rec")
    a = ap.parse_args()

    n = extract(a.video, a.outdir, a.frames_every)
    print(f"frames: {n} -> {a.outdir}/frames")

    wav = f"{a.outdir}/audio.wav"
    if not has_audio(wav):
        print("NO AUDIO TRACK in this recording - visual review only.")
        return

    try:
        lines = transcribe(wav)
    except Exception as e:
        print(f"transcribe unavailable ({e}) - visual review only.")
        return

    path = f"{a.outdir}/transcript.txt"
    with open(path, "w") as f:
        f.write("\n".join(lines) or "(no speech decoded)")
    print(f"transcript: {path}")
    print("\n".join(lines) if lines else "(no speech decoded - silence or music)")
    print("\nBEST-EFFORT ONLY. Small offline model: partial words expected. "
          "Confirm anything load-bearing in text before carding it.")


if __name__ == "__main__":
    main()
