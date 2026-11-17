"""Gentle additional motion using the same full-colour oversampling policy."""

from render.zoom import zoom_filter


def motion_filter(frames, motion):
    if frames < 2:
        raise ValueError("At least two frames required.")
    if motion in ("slow zoom in", "slow zoom out"):
        return zoom_filter(frames, motion)
    if motion == "static":
        return f"scale=1280:720:flags=lanczos,zoompan=z=1:x=0:y=0:d={frames}:s=1280x720:fps=25,format=yuv420p"
    t = f"min(on/{frames-1},1)"
    ease = f"({t})*({t})*(3-2*({t}))"
    x, y = "(iw-trunc(iw/zoom))/2", "(ih-trunc(ih/zoom))/2"
    if motion in ("pan right", "pan left"):
        factor = ease if motion == "pan right" else f"1-({ease})"
        x = f"(iw-trunc(iw/zoom))*({factor})"
    elif motion in ("pan up", "pan down"):
        factor = ease if motion == "pan down" else f"1-({ease})"
        y = f"(ih-trunc(ih/zoom))*({factor})"
    else:
        raise ValueError(f"Unsupported motion: {motion}")
    return (
        "format=yuv444p,scale=3840:2160:flags=lanczos,format=yuv444p,"
        f"zoompan=z=1.06:x='{x}':y='{y}':d={frames}:s=1280x720:fps=25,"
        "format=yuv444p,format=yuv420p"
    )
