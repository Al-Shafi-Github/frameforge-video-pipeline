"""Deterministic zoom expressions; no media processing on the local computer."""

FPS = 25


def zoom_filter(frames: int, motion: str, working_width: int = 3840) -> str:
    if frames < 2:
        raise ValueError("Zoom needs at least two frames.")
    if working_width not in (3840, 7680):
        raise ValueError("Use the tested 4K or 8K working canvas.")
    height = working_width * 9 // 16
    progress = f"min(on/{frames - 1},1)"
    # Smoothstep: starts/ends gently; reaches the endpoint on the last frame.
    ease = f"({progress})*({progress})*(3-2*({progress}))"
    zoom = f"1.1-0.1*({ease})" if motion == "slow zoom out" else f"1+0.1*({ease})"
    return (
        f"format=yuv444p,scale={working_width}:{height}:flags=lanczos,format=yuv444p,"
        f"zoompan=z='{zoom}':"
        # zoompan truncates crop dimensions to integers internally. Centre that
        # actual crop, not the fractional ideal crop, to limit centre wobble.
        "x='(iw-trunc(iw/zoom))/2':y='(ih-trunc(ih/zoom))/2':"
        f"d={frames}:s=1280x720:fps={FPS},format=yuv444p,format=yuv420p"
    )


def zoom_at(frame: int, frames: int, motion: str) -> float:
    if frames < 2 or not 0 <= frame < frames:
        raise ValueError("Invalid frame range.")
    progress = frame / (frames - 1)
    ease = progress * progress * (3 - 2 * progress)
    return 1.1 - .1 * ease if motion == "slow zoom out" else 1 + .1 * ease
