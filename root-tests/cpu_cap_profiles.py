"""Build TuneD overlays for a CPU ceiling without replacing power-profile policy."""
import configparser
import io
import re
from pathlib import Path

PREFIX = "rog-cpu-cap-"


def build(ppd_text, cap, resolve):
    if isinstance(cap, bool) or not isinstance(cap, int) or not 40 <= cap <= 100:
        raise ValueError("CAP must be an integer from 40 to 100")
    cfg = configparser.ConfigParser(interpolation=None, inline_comment_prefixes=("#",))
    cfg.read_string(ppd_text)
    if not cfg.has_section("profiles") or not all(k in cfg["profiles"] for k in ("performance", "balanced", "power-saver")):
        raise ValueError("ppd.conf must map all three power profiles")
    generated = {}
    for section in ("profiles", "battery"):
        if not cfg.has_section(section):
            continue
        for mode, original in list(cfg[section].items()):
            if not re.fullmatch(r"[A-Za-z0-9_.-]+", mode) or not re.fullmatch(r"[A-Za-z0-9_.-]+", original) or original.startswith(PREFIX):
                raise ValueError("Unsupported or recursively capped profile mapping")
            units = resolve(original)
            overlay = configparser.ConfigParser(interpolation=None)
            overlay["main"] = {"summary": f"CPU ceiling {cap}% over {original}", "include": original}
            if not units:
                raise ValueError(f"No CPU policy in {original}")
            for name, options in units.items():
                # The old helper's max is the user's previous cap, not an independent limit.
                maximum = cap if original == "rog-performance" else min(cap, _pct(options.get("max_perf_pct"), 100))
                minimum = min(maximum, _pct(options.get("min_perf_pct"), 0))
                overlay[name] = {"min_perf_pct": str(minimum), "max_perf_pct": str(maximum)}
                # Preserve custom instance type if the unit is not named [cpu].
                if name != "cpu":
                    overlay[name]["type"] = "cpu"
            text = io.StringIO()
            overlay.write(text)
            name = f"{PREFIX}{section}-{mode}"
            generated[name] = text.getvalue()
            cfg[section][mode] = name
    text = io.StringIO()
    cfg.write(text)
    return text.getvalue(), generated


def _pct(value, default):
    if value in (None, ""):
        return default
    if not re.fullmatch(r"[0-9]+", str(value)) or not 0 <= int(value) <= 100:
        raise ValueError(f"Unsupported CPU percentage: {value!r}")
    return int(value)


def tuned_resolve(name):
    # Use TuneD's own inheritance, unit merging and variable expansion rules.
    from tuned.profiles.loader import Loader
    from tuned.profiles.locator import Locator
    from tuned.profiles.factory import Factory
    from tuned.profiles.merger import Merger
    from tuned.profiles.variables import Variables
    variables = Variables()
    loader = Loader(Locator(["/usr/lib/tuned/profiles", "/etc/tuned/profiles"]), Factory(), Merger(), None, variables)
    profile = loader.load(name)
    return {key: {k: variables.expand(v) for k, v in unit.options.items() if k in ("min_perf_pct", "max_perf_pct")}
            for key, unit in profile.units.items() if unit.type == "cpu" and unit.enabled}


if __name__ == "__main__":
    import sys
    # Produce all files in a staging directory before the caller changes live mappings.
    source, destination, requested = sys.argv[1:]
    mapping, profiles = build(Path(source).read_text(), int(requested), tuned_resolve)
    target = Path(destination)
    target.mkdir()
    (target / "ppd.conf").write_text(mapping)
    for name, text in profiles.items():
        (target / name).mkdir()
        (target / name / "tuned.conf").write_text(text)
