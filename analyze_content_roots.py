"""
Walks user-specified content directories, finds all .duf files, checks which
are tracked in the DAZ CMS PostgreSQL database, and groups untracked files
into logical products.

Usage:
    python analyze_content_roots.py [DIR1 DIR2 ...]

If no directories are given, uses the base paths from the DAZ CMS database.
"""

import os
import argparse
import psycopg2
import psycopg2.extras
from pathlib import Path
from collections import defaultdict
from dotenv import load_dotenv

load_dotenv()

# ─── Path classification ───────────────────────────────────────────────────────

FIGURE_NAMES = {
    "genesis 9 female", "genesis 9 male", "genesis 9",
    "genesis 8.1 female", "genesis 8.1 male", "genesis 8.1",
    "genesis 8 female", "genesis 8 male", "genesis 8",
    "genesis 3 female", "genesis 3 male", "genesis 3",
    "genesis 2 female", "genesis 2 male", "genesis 2",
    "genesis female", "genesis male", "genesis",
    "victoria 4", "michael 4", "v4", "m4",
    "aiko 6", "victoria 6", "michael 6",
}

RUNTIME_TYPES = {
    "character", "characters", "material", "materials",
    "pose", "poses", "prop", "props", "hair",
    "light", "lights", "camera", "cameras", "scene", "scenes", "hand",
}

CATEGORY_WORDS = {
    "clothing", "characters", "poses", "hair", "props",
    "materials", "anatomy", "environments", "lights", "accessories",
    "shaders", "clothing and accessories",
}


def is_runtime_path(parts):
    return (len(parts) >= 2
            and parts[0].lower() == "runtime"
            and parts[1].lower() == "libraries")


def runtime_group_key(parts):
    """Group key for Runtime/Libraries paths: (normalised_brand, leaf_name_lower)."""
    inner = parts[2:]  # strip Runtime/Libraries
    if inner and inner[0].lower() in RUNTIME_TYPES:
        inner = inner[1:]  # strip type
    if not inner:
        return parts
    brand_norm = inner[0].lower().rstrip("s")  # normalise plural forms
    leaf = inner[-1].lower() if len(inner) > 1 else ""
    return (brand_norm, leaf)


def modern_group_key(parts):
    """Group key for modern paths: normalise figure names so G8/G9 paths merge."""
    return tuple("[FIGURE]" if p.lower() in FIGURE_NAMES else p for p in parts)


# ─── Database helpers ──────────────────────────────────────────────────────────

def connect():
    return psycopg2.connect(
        dbname=os.environ["DB_NAME"],
        user=os.environ["DB_USER"],
        password=os.environ.get("DB_PASS", ""),
        host=os.environ["DB_HOST"],
        port=os.environ["DB_PORT"],
    )


def load_db_base_paths(conn):
    with conn.cursor() as cur:
        cur.execute('SELECT "fldBasePath" FROM dzcontent."tblBasePath" ORDER BY "RecID"')
        return [r[0] for r in cur.fetchall()]


def load_tracked_files(conn):
    """Returns set of (normalised_path, lower_filename) for every DB entry."""
    print("Loading tracked files from PostgreSQL...", end=" ", flush=True)
    with conn.cursor(cursor_factory=psycopg2.extras.DictCursor) as cur:
        cur.execute("SELECT path, filename FROM dzcontent.content")
        tracked = set()
        for row in cur:
            norm_path = row["path"].replace("\\", "/").lower().rstrip("/")
            tracked.add((norm_path, row["filename"].lower()))
    print(f"{len(tracked):,} entries loaded.")
    return tracked


# ─── Filesystem walk ───────────────────────────────────────────────────────────

def walk_duf_files(root):
    """Yields all user-facing .duf files under root, skipping the data/ subtree."""
    result = []
    for dirpath, dirnames, filenames in os.walk(root):
        rel = Path(dirpath).relative_to(root)
        if rel.parts and rel.parts[0].lower() == "data":
            dirnames.clear()
            continue
        for fname in filenames:
            if fname.lower().endswith(".duf"):
                result.append(Path(dirpath) / fname)
    return result


def is_tracked(file, root, tracked):
    rel = file.relative_to(root)
    return (str(rel.parent).replace("\\", "/").lower(), rel.name.lower()) in tracked


# ─── Product grouping ──────────────────────────────────────────────────────────

def merge_into_ancestors(dir_stats, content_roots):
    """
    Pass 1: roll subdirectories into their shallowest common ancestor.

    Alternates between two sub-passes until stable:
      A. Absorb: if any root is a subdirectory of another root, merge it in.
      B. Synthesize: if multiple sibling roots share a parent not yet in the
         root set, create that parent as a new root (handles products whose
         top-level folder has no .duf files directly).

    Synthesis stops when the proposed parent would be shallower than
    MIN_PRODUCT_DEPTH components below the content root, preventing the
    algorithm from merging unrelated products under a broad category dir.
    """
    # Min depth a synthesized parent must have relative to its content root.
    # 5 lets products sit at People/{fig}/{cat}/{vendor}/{product} (depth 5)
    # while preventing over-merging at the vendor/category level (depth 4).
    MIN_PRODUCT_DEPTH = 5

    # Identify the content root each dir belongs to (for depth calculation)
    def depth_from_root(path):
        for cr in content_roots:
            if str(path).startswith(str(cr)):
                return len(path.relative_to(cr).parts)
        return len(path.parts)

    # Start: each dir is its own root, mapped to {itself}
    roots = {d: {d} for d in dir_stats}

    changed = True
    while changed:
        changed = False

        # Sub-pass A: absorb any root that sits inside another root
        for d in sorted(roots, key=lambda p: len(p.parts), reverse=True):
            if d not in roots:
                continue
            for ancestor in list(roots):
                if ancestor == d:
                    continue
                if str(d).startswith(str(ancestor) + os.sep):
                    roots[ancestor] |= roots.pop(d)
                    changed = True
                    break
            if changed:
                break

        if changed:
            continue

        # Sub-pass B: synthesize a parent for sibling roots that share one
        parent_map = defaultdict(list)
        for root in roots:
            parent_map[root.parent].append(root)

        # Process deepest parents first so we build bottom-up
        for parent in sorted(parent_map, key=lambda p: len(p.parts), reverse=True):
            siblings = parent_map[parent]
            if len(siblings) < 2:
                continue
            if parent in roots:
                continue
            if depth_from_root(parent) < MIN_PRODUCT_DEPTH:
                continue  # too shallow — don't merge unrelated dirs
            # Don't synthesize generic content category directories.
            # e.g. Characters/Effie + Simona + Tamara should stay separate,
            # but G8G9CouplesPoses/Anywhere + InPlace should merge.
            if parent.name.lower() in CATEGORY_WORDS:
                continue
            merged_set = set()
            for sib in siblings:
                merged_set |= roots.pop(sib)
            roots[parent] = merged_set
            changed = True
            break

    return {
        root: {
            "file_count": sum(
                dir_stats[d]["untracked"] for d in constituents if d in dir_stats
            ),
            "constituent_dirs": sorted(constituents),
        }
        for root, constituents in roots.items()
    }


def group_into_products(merged, content_roots):
    """
    Pass 2+3: group merged product roots across figures (modern) and Runtime types.
    Returns list of product dicts sorted by vendor then name.
    """
    buckets = defaultdict(list)

    for root, data in merged.items():
        cr = next((c for c in content_roots if str(root).startswith(str(c))), None)
        if cr is None:
            continue
        rel_parts = root.relative_to(cr).parts

        if is_runtime_path(rel_parts):
            key = ("runtime", runtime_group_key(rel_parts))
        else:
            key = ("modern", modern_group_key(rel_parts))

        buckets[key].append((root, data))

    products = []
    for (path_type, _group_key), entries in buckets.items():
        total_files = sum(d["file_count"] for _, d in entries)
        all_dirs = []
        for _, d in entries:
            all_dirs.extend(d["constituent_dirs"])

        rep_root = min((root for root, _ in entries), key=lambda p: len(p.parts))
        cr = next((c for c in content_roots if str(rep_root).startswith(str(c))), None)
        rel_parts = rep_root.relative_to(cr).parts if cr else rep_root.parts
        name, vendor = _infer_name_vendor(rel_parts, path_type)

        context_labels = set()
        for root, _ in entries:
            _cr = next((c for c in content_roots if str(root).startswith(str(c))), None)
            rp = root.relative_to(_cr).parts if _cr else root.parts
            lbl = _context_label(rp, path_type)
            if lbl:
                context_labels.add(lbl)

        products.append({
            "name": name,
            "vendor": vendor,
            "file_count": total_files,
            "context": sorted(context_labels),
            "paths": sorted(root for root, _ in entries),
            "all_dirs": sorted(set(all_dirs)),
        })

    return sorted(products, key=lambda p: (p["vendor"] or "", p["name"]))


def _infer_name_vendor(rel_parts, path_type):
    if path_type == "runtime":
        inner = rel_parts[2:]  # strip Runtime/Libraries
        if inner and inner[0].lower() in RUNTIME_TYPES:
            inner = inner[1:]  # strip type
        if not inner:
            return ("(unknown)", None)
        if len(inner) == 1:
            return (inner[0], None)
        name = inner[-1]
        vendor = next(
            (p for p in reversed(inner[:-1]) if p.lower() not in CATEGORY_WORDS),
            None
        )
        return (name, vendor)
    else:
        # Modern: {top}/{figure}/{category}/{vendor?}/{product}
        fig_idx = next(
            (i for i, p in enumerate(rel_parts) if p.lower() in FIGURE_NAMES), None
        )
        after_fig = rel_parts[fig_idx + 1:] if fig_idx is not None else rel_parts[1:]
        if not after_fig:
            return (rel_parts[-1] if rel_parts else "(unknown)", None)
        if len(after_fig) == 1:
            return (after_fig[0], None)
        # after_fig[0] is the content category (Poses, Clothing, ...)
        remainder = after_fig[1:]  # vendor?, product
        if not remainder:
            return (after_fig[0], None)
        if len(remainder) == 1:
            return (remainder[0], None)
        # remainder[0] is vendor, remainder[-1] is product
        return (remainder[-1], remainder[0])


def _context_label(rel_parts, path_type):
    if path_type == "runtime":
        return rel_parts[2] if len(rel_parts) >= 3 else ""
    return next((p for p in rel_parts if p.lower() in FIGURE_NAMES), "")


# ─── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dirs", nargs="*",
                        help="Content roots to scan (default: from DAZ CMS database)")
    parser.add_argument("--show-dirs", action="store_true",
                        help="List every constituent directory under each product")
    args = parser.parse_args()

    conn = connect()
    scan_roots = (
        [Path(d) for d in args.dirs]
        if args.dirs
        else [Path(p) for p in load_db_base_paths(conn)]
    )

    print("\nContent roots to scan:")
    for r in scan_roots:
        print(f"  {'[OK]' if r.exists() else '[MISSING]'} {r}")
    scan_roots = [r for r in scan_roots if r.exists()]
    if not scan_roots:
        print("No accessible content roots. Exiting.")
        conn.close()
        return

    tracked = load_tracked_files(conn)
    conn.close()

    total_files = tracked_files = untracked_files = 0
    dir_stats = defaultdict(lambda: {"tracked": 0, "untracked": 0})

    for root in scan_roots:
        print(f"\nScanning {root} ...")
        dufs = walk_duf_files(root)
        print(f"  Found {len(dufs):,} .duf files")
        for f in dufs:
            total_files += 1
            if is_tracked(f, root, tracked):
                tracked_files += 1
                dir_stats[f.parent]["tracked"] += 1
            else:
                untracked_files += 1
                dir_stats[f.parent]["untracked"] += 1

    fully_untracked = {d: v for d, v in dir_stats.items() if v["tracked"] == 0}
    mixed           = {d: v for d, v in dir_stats.items()
                       if v["tracked"] > 0 and v["untracked"] > 0}

    sep = "-" * 44
    pct = lambda n: f"{n/total_files*100:.1f}%" if total_files else "0%"
    print(f"""
{sep}
 File summary
{sep}
 Total .duf files:          {total_files:>7,}
   Tracked in DAZ CMS DB:   {tracked_files:>7,}  ({pct(tracked_files)})
   NOT tracked in DB:        {untracked_files:>7,}  ({pct(untracked_files)})
{sep}""")

    merged_untracked = merge_into_ancestors(fully_untracked, scan_roots)
    products         = group_into_products(merged_untracked, scan_roots)

    merged_mixed     = merge_into_ancestors(mixed, scan_roots)
    mixed_products   = group_into_products(merged_mixed, scan_roots)

    print(f"""
{sep}
 Product grouping (untracked files only)
{sep}
 Raw untracked directories:  {len(fully_untracked):>5}
 Logical products identified:{len(products):>5}
 Mixed products (partial):   {len(mixed_products):>5}
{sep}""")

    if products:
        print(f"\nFully untracked products ({len(products)}):\n")
        print(f"  {'Files':>5}  {'Vendor / Artist':<28}  Product")
        print(f"  {'-'*5}  {'-'*28}  {'-'*30}")
        for p in products:
            vendor_str = (p["vendor"] or "").ljust(28)
            ctx = f"  [{', '.join(p['context'])}]" if p["context"] else ""
            print(f"  {p['file_count']:>5}  {vendor_str}  {p['name']}{ctx}")
            if args.show_dirs:
                for d in p["all_dirs"]:
                    print(f"         {'':28}    {d}")

    if mixed_products:
        print(f"\nMixed products — some files tracked, some not ({len(mixed_products)}):\n")
        for p in mixed_products:
            vendor_str = p["vendor"] or "(unknown)"
            print(f"  {p['file_count']:>4} untracked  {vendor_str} / {p['name']}")


if __name__ == "__main__":
    main()
