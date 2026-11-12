#!/usr/bin/env python3
"""_site_query.py — read one field for one TARGET from sites.yaml.

Usage: _site_query.py <sites.yaml> <target_name> <field>

<field> is a top-level target key (name, ssh_name, scheduler, mode, mpi_ranks,
gpus_per_node, mpi_launch), or one of two synthetics:
  env_load_joined   -> "cmd1 && cmd2" verbatim (or "true" if none)
  directives        -> the verbatim multi-line #SBATCH/#PBS block, as-is

Uses PyYAML when available (robust); otherwise a minimal fallback parser that
understands the specific shape of this file, INCLUDING the `directives: |` block
scalar. The fallback exists so the repo clones and runs with no pip install.
"""
import sys


def load_yaml(path):
    try:
        import yaml  # type: ignore
        with open(path) as fh:
            return yaml.safe_load(fh)
    except ImportError:
        return _fallback_parse(path)


def _fallback_parse(path):
    """Parse the target list, handling one level of nested list (env_load) and
    the `directives: |` block scalar. Not a general YAML parser."""
    with open(path) as fh:
        lines = fh.readlines()

    targets = []
    cur = None
    i = 0
    n = len(lines)
    while i < n:
        raw = lines[i].rstrip("\n")
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))

        # Skip blank and comment lines (comments only when not inside a block).
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        if stripped.startswith("- name:"):
            cur = {"name": stripped.split(":", 1)[1].strip()}
            targets.append(cur)
            i += 1
            continue

        if cur is None:
            i += 1
            continue

        # Block scalar: `directives: |`
        if stripped in ("directives: |", "directives: |-", "directives:|"):
            block_indent = indent + 2
            i += 1
            block_lines = []
            while i < n:
                bl = lines[i].rstrip("\n")
                if bl.strip() == "":
                    block_lines.append("")
                    i += 1
                    continue
                bindent = len(bl) - len(bl.lstrip(" "))
                if bindent < block_indent:
                    break
                block_lines.append(bl[block_indent:])
                i += 1
            # Trim trailing blank lines.
            while block_lines and block_lines[-1] == "":
                block_lines.pop()
            cur["directives"] = "\n".join(block_lines)
            continue

        # Nested list item — append to the most recently opened list key.
        if stripped.startswith("- "):
            key = cur.get("_list_key")
            if key:
                cur.setdefault(key, [])
                cur[key].append(stripped[2:].strip())
            i += 1
            continue

        # `key:` that opens a list (e.g. env_load:)
        if stripped.endswith(":") and stripped[:-1] != "":
            key = stripped[:-1].strip()
            cur["_list_key"] = key
            cur.setdefault(key, [])
            i += 1
            continue

        # Simple `key: value`
        if ":" in stripped:
            k, v = stripped.split(":", 1)
            cur[k.strip()] = v.strip().strip('"')
            cur.pop("_list_key", None)
            i += 1
            continue

        i += 1

    return {"targets": targets}


def main():
    if len(sys.argv) != 4:
        print("", end="")
        return 2
    path, target, field = sys.argv[1], sys.argv[2], sys.argv[3]
    data = load_yaml(path)
    targets = data.get("targets", []) if isinstance(data, dict) else []
    entry = next((t for t in targets if t.get("name") == target), None)
    if entry is None:
        print("", end="")
        return 0

    if field == "env_load_joined":
        cmds = entry.get("env_load") or []
        if isinstance(cmds, dict):
            cmds = []
        # Join verbatim with && — NO prefixing. Each entry is a full command.
        print(" && ".join(cmds) or "true", end="")
        return 0

    val = entry.get(field)
    print("" if val is None else val, end="")
    return 0


if __name__ == "__main__":
    sys.exit(main())
