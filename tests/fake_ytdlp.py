"""A scripted stand-in for yt-dlp, for download-lifecycle tests.

    fake_ytdlp.py ok <outfile>   progress lines, writes <outfile>, prints its path, exit 0
    fake_ytdlp.py fail           an ERROR line, exit 1
    fake_ytdlp.py archived       the archive-skip line, exit 0
    fake_ytdlp.py hang           a progress line, then sleeps until killed
    fake_ytdlp.py tracks <dir>   writes two album tracks, reports them as [YTFILE] lines
"""

import json
import os
import sys
import time


def say(line: str) -> None:
    sys.stdout.write(line + "\n")
    sys.stdout.flush()


def main() -> int:
    mode = sys.argv[1]
    if mode == "ok":
        out = sys.argv[2]
        say("[download] Destination: " + out)
        for done in (0, 2500, 5000, 7500, 10000):
            say(f"[YTPROG] {done}|10000|None|1000.0|{(10000 - done) / 1000}|downloading|mp4")
        say("[YTPROG] 10000|10000|None|1000.0|0|finished|mp4")
        say("[YTPP] Merger")
        with open(out, "wb") as fh:
            fh.write(b"x" * 10000)
        say("filepath:" + out)
        return 0
    if mode == "fail":
        say("ERROR: [youtube] v: Video unavailable")
        return 1
    if mode == "archived":
        say("[download] v: v has already been recorded in the archive")
        return 0
    if mode == "tracks":
        folder = sys.argv[2]
        os.makedirs(folder, exist_ok=True)
        say("[YTPROG] 100|100|None|1.0|0|finished|mp3")
        say("filepath:" + os.path.join(folder, "Album.mp3"))     # the unsplit file, printed first
        say("[YTPP] ChiveSplit")
        for n, title in ((1, "One"), (2, "Two")):
            path = os.path.join(folder, f"{n:02d} - {title}.mp3")
            with open(path, "wb") as fh:
                fh.write(b"x" * 500)
            say("[YTFILE] " + json.dumps({"path": path, "title": title, "track": n, "duration": 10}))
        return 0
    if mode == "hang":
        say("[YTPROG] 100|10000|None|10.0|990|downloading|mp4")
        time.sleep(60)
        return 0
    return 2


if __name__ == "__main__":
    sys.exit(main())
