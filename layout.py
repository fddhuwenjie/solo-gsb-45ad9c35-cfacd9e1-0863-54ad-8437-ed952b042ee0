"""前面板走线排版求解。

在既有路由规划（solver.plan_all）确定的信号拓扑、常态模式与跳线端点
保持不变的前提下，把用户划分的面板分区（可用走线槽层 + 固定插孔列位）
套用到已安装跳线上，为每根跳线分配走线槽层与左右绕行方向，按字典序优化：

  1. 交叉数最少 —— 同一槽层内，两根跳线的水平区间在上/下插孔行之间出现
     逆序（一根的 T 列靠前而 B 列靠后）即计一次交叉；不同槽层在物理上
     前后错开，不计交叉；
  2. 跨区线数最少 —— 端点分属不同分区的跳线数量。列位由用户固定后该值
     是定值，仅作为并列时的次序参考（见 README）；
  3. 同层长度最短 —— 水平跨度 + 槽层深度 + 反向绕行附加（0.5 列距）。

硬约束：
  - 跳线水平跨度经过的每个分区都必须提供所选槽层（公共槽层），
    否则该跳线无法分配；
  - 每个分区内每层槽的容量有限（默认 DEFAULT_CAPACITY，可按分区配置）；
  - 同层相邻列的跳线进槽方向不得对撞（可经方向调整消除，不计入冲突）。

无可分配槽层的跳线、以及优化后仍残留的同层交叉，作为“无法避免的冲突”
随结果一并输出。
"""

from solver import ConfigError, plan_all

DEFAULT_CAPACITY = 8
NODE_CAP = 300000                    # 精确搜索节点上限，超出回退贪心结果
DIRECTION_LABELS = {"L": "左绕", "R": "右绕"}


class LayoutError(ConfigError):
    """走线排版配置非法。"""

    def __init__(self, message, details=None):
        super().__init__(message, code="LAYOUT_INVALID", details=details)


# ---- 配置解析 -------------------------------------------------------------


def parse_layout(raw, pair_ids):
    """校验 layout 配置，返回 (zones, col_of, layer_depth, cap_of)。

    zones:        [{index, name, columns:[pairId...], layers:[{id, capacity}], range:[lo,hi]}]
    col_of:       插孔对编号 -> 全局列序号（按分区声明顺序从左到右编号）
    layer_depth:  槽层编号 -> 深度序号（按首次出现顺序，越靠前越浅）
    cap_of:       (分区序号, 槽层编号) -> 容量
    """
    if not isinstance(raw, dict):
        raise LayoutError("layout 必须是 JSON 对象，形如 {\"zones\": [...]}")
    raw_zones = raw.get("zones")
    if not isinstance(raw_zones, list) or not raw_zones:
        raise LayoutError("layout.zones 必须是非空数组")

    pair_set = set(pair_ids)
    zones, col_of, layer_depth, cap_of = [], {}, {}, {}
    seen_cols = set()
    for zi, z in enumerate(raw_zones):
        if not isinstance(z, dict):
            raise LayoutError(f"第 {zi + 1} 个分区必须是 JSON 对象")
        name = str(z.get("name") or f"分区{zi + 1}").strip()
        cols = z.get("columns")
        if not isinstance(cols, list) or not cols:
            raise LayoutError(f"分区 “{name}”：columns 必须是非空的插孔对编号数组（左→右）")
        norm_cols = []
        for c in cols:
            try:
                c = int(c)
            except (TypeError, ValueError):
                raise LayoutError(f"分区 “{name}”：列位 {c!r} 不是整数编号")
            if c not in pair_set:
                raise LayoutError(f"分区 “{name}”：插孔对 {c} 在配置中不存在")
            if c in seen_cols:
                raise LayoutError(f"插孔对 {c} 被多个分区重复占用")
            seen_cols.add(c)
            norm_cols.append(c)

        layers = z.get("layers")
        if not isinstance(layers, list) or not layers:
            raise LayoutError(f"分区 “{name}”：layers 必须是非空数组，"
                              f"如 [1, 2] 或 [{{\"id\": 1, \"capacity\": 6}}]")
        norm_layers, seen_l = [], set()
        for l in layers:
            lid, cap = (l.get("id"), l.get("capacity", DEFAULT_CAPACITY)) \
                if isinstance(l, dict) else (l, DEFAULT_CAPACITY)
            try:
                lid, cap = int(lid), int(cap)
            except (TypeError, ValueError):
                raise LayoutError(f"分区 “{name}”：槽层编号与容量必须为整数")
            if lid < 1:
                raise LayoutError(f"分区 “{name}”：槽层编号必须 ≥ 1")
            if cap < 1:
                raise LayoutError(f"分区 “{name}”：槽层 {lid} 的容量必须 ≥ 1")
            if lid in seen_l:
                raise LayoutError(f"分区 “{name}”：槽层 {lid} 重复定义")
            seen_l.add(lid)
            if lid not in layer_depth:
                layer_depth[lid] = len(layer_depth) + 1
            norm_layers.append({"id": lid, "capacity": cap})
            cap_of[(zi, lid)] = cap
        zones.append({"index": zi, "name": name,
                      "columns": norm_cols, "layers": norm_layers})

    missing = [pid for pid in pair_ids if pid not in seen_cols]
    if missing:
        raise LayoutError(f"插孔对 {missing} 未分配列位：所有插孔对都必须且只能属于一个分区",
                          details={"missing": missing})

    col = 0
    for z in zones:
        for pid in z["columns"]:
            col += 1
            col_of[pid] = col
        z["range"] = [col_of[z["columns"][0]], col_of[z["columns"][-1]]]
    return zones, col_of, layer_depth, cap_of


# ---- 跳线几何 -------------------------------------------------------------


def _cable_infos(cables, col_of, zones, layer_depth):
    """跳线端点 → 列位/分区/可用槽层/水平跨度。

    上插孔（T，接输出）在面板上行，下插孔（B，接输入）在下行；
    跳线从 T 列垂入走线槽，水平走到 B 列再升起。
    """
    def zone_of(col):
        for z in zones:
            if z["range"][0] <= col <= z["range"][1]:
                return z["index"]
        raise AssertionError("列位未覆盖")

    infos = []
    for c in cables:
        j1, j2 = c["fromJack"], c["toJack"]
        t_jack, b_jack = (j1, j2) if j1.endswith("T") else (j2, j1)
        a, b = col_of[int(t_jack[:-1])], col_of[int(b_jack[:-1])]
        lo, hi = min(a, b), max(a, b)
        zspan = [z["index"] for z in zones
                 if z["range"][1] >= lo and z["range"][0] <= hi]
        common = None
        for zi in zspan:
            ids = {l["id"] for l in zones[zi]["layers"]}
            common = ids if common is None else (common & ids)
        infos.append({
            "cid": c["id"], "fromJack": j1, "toJack": j2,
            "fromPort": c["fromPort"], "toPort": c["toPort"],
            "a": a, "b": b, "span": hi - lo,
            "zones": zspan,
            "fromZone": zone_of(a), "toZone": zone_of(b),
            "layers": sorted(common, key=lambda l: (layer_depth[l], l)),
        })
    return infos


def _inversions(infos):
    """端点列序逆序对：同层即交叉（同列不可能：每个插孔至多一根跳线）。"""
    pairs = []
    for i in range(len(infos)):
        for j in range(i + 1, len(infos)):
            x, y = infos[i], infos[j]
            if (x["a"] - y["a"]) * (x["b"] - y["b"]) < 0:
                pairs.append((i, j))
    return pairs


# ---- 槽层分配 -------------------------------------------------------------


def _greedy_assign(infos, cap_of):
    """基线（优化前）：按跳线编号顺序，放入最浅的容量未满公共槽层。"""
    used = {}
    assign = [None] * len(infos)
    for k, info in enumerate(infos):
        for L in info["layers"]:
            if all(used.get((z, L), 0) < cap_of[(z, L)] for z in info["zones"]):
                assign[k] = L
                for z in info["zones"]:
                    used[(z, L)] = used.get((z, L), 0) + 1
                break
    return assign


def _full_key(infos, inv_pairs, assign, layer_depth):
    """完整分配的目标键：(未分配数, 交叉数, 基础长度)。跨区线数为定值不参与比较。"""
    un = sum(1 for a in assign if a is None)
    cr = sum(1 for i, j in inv_pairs
             if assign[i] is not None and assign[i] == assign[j])
    le = sum(infos[k]["span"] + layer_depth[assign[k]]
             for k in range(len(infos)) if assign[k] is not None)
    return (un, cr, le)


def _optimize_assign(infos, cap_of, layer_depth, inv_pairs):
    """精确字典序搜索（未分配数 → 交叉数 → 基础长度），带剪枝与节点上限。

    返回 (assign, exhausted)：exhausted=False 表示超出上限，结果可能非最优。
    """
    n = len(infos)
    partners = [[] for _ in infos]
    for i, j in inv_pairs:
        partners[i].append(j)
        partners[j].append(i)
    # 可选槽层少、跨分区多的跳线先定，剪枝更有效
    order = sorted(range(n),
                   key=lambda i: (len(infos[i]["layers"]), -len(infos[i]["zones"]),
                                  infos[i]["cid"]))
    suf = [0] * (n + 1)                      # 后缀最小基础长度（下界）
    for k in range(n - 1, -1, -1):
        info = infos[order[k]]
        best_l = min((info["span"] + layer_depth[l] for l in info["layers"]),
                     default=0)
        suf[k] = suf[k + 1] + best_l

    greedy = _greedy_assign(infos, cap_of)
    best_key = [_full_key(infos, inv_pairs, greedy, layer_depth)]
    best_assign = [list(greedy)]
    used, assign = {}, [None] * n
    nodes, exhausted = [0], [True]

    def dfs(k, un, cr, le):
        if nodes[0] >= NODE_CAP:
            exhausted[0] = False
            return
        nodes[0] += 1
        bk = best_key[0]
        if un > bk[0] or (un == bk[0] and (cr > bk[1]
                                           or (cr == bk[1] and le + suf[k] >= bk[2]))):
            return
        if k == n:
            key = (un, cr, le)
            if key < bk:
                best_key[0] = key
                best_assign[0] = list(assign)
            return
        i = order[k]
        info = infos[i]
        feasible = False
        for L in info["layers"]:
            if any(used.get((z, L), 0) >= cap_of[(z, L)] for z in info["zones"]):
                continue
            feasible = True
            d = sum(1 for j in partners[i] if assign[j] == L)
            for z in info["zones"]:
                used[(z, L)] = used.get((z, L), 0) + 1
            assign[i] = L
            dfs(k + 1, un, cr + d, le + info["span"] + layer_depth[L])
            assign[i] = None
            for z in info["zones"]:
                used[(z, L)] -= 1
        if not feasible:
            # 容量/公共槽层不足：该跳线只能不分配（未分配数是第一优先级，
            # 有可放槽层时主动不分配绝不会更优，无需展开该分支）
            dfs(k + 1, un + 1, cr, le)

    dfs(0, 0, 0, 0)
    return best_assign[0], exhausted[0]


# ---- 绕行方向 -------------------------------------------------------------


def _toward(info):
    return "R" if info["b"] >= info["a"] else "L"


def _dir_key(group, bits):
    """方向方案的评价键：(对撞数, 反向绕行附加数, 与首选方向不一致数)。"""
    occ, extra, ham = {}, 0, 0
    for g, bit in zip(group, bits):
        d = "R" if bit else "L"
        if g["a"] != g["b"] and d != _toward(g):
            extra += 1
        if d != _toward(g):
            ham += 1
        for row, col in (("T", g["a"]), ("B", g["b"])):
            gap = col if d == "R" else col - 1
            occ[(row, gap)] = occ.get((row, gap), 0) + 1
    clash = sum(v * (v - 1) // 2 for v in occ.values())
    return clash, extra, ham


def _assign_directions(group):
    """同层一组跳线的左右绕行方向：先消除相邻列进槽对撞，再最小化反向绕行。"""
    k = len(group)
    if k <= 16:
        best_bits, best_key = None, None
        for mask in range(1 << k):
            bits = [(mask >> t) & 1 for t in range(k)]
            key = _dir_key(group, bits)
            if best_key is None or key < best_key:
                best_key, best_bits = key, bits
    else:                                   # 大组：从首选方向出发迭代翻转
        bits = [1 if _toward(g) == "R" else 0 for g in group]
        best_bits, best_key = list(bits), _dir_key(group, bits)
        for _ in range(8 * k):
            improved = False
            for t in range(k):
                bits[t] ^= 1
                key = _dir_key(group, bits)
                if key < best_key:
                    best_key, best_bits = key, list(bits)
                    improved = True
                else:
                    bits[t] ^= 1
            if not improved:
                break
    return {g["cid"]: ("R" if b else "L") for g, b in zip(group, best_bits)}


def _gap_conflicts(infos, assign, dirs):
    """同层、同行、同槽隙的方向对撞数（优化结果应恒为 0）。"""
    occ = {}
    for k, info in enumerate(infos):
        L = assign[k]
        if L is None:
            continue
        d = dirs.get(info["cid"], "R")
        for row, col in (("T", info["a"]), ("B", info["b"])):
            gap = col if d == "R" else col - 1
            occ[(L, row, gap)] = occ.get((L, row, gap), 0) + 1
    return sum(v * (v - 1) // 2 for v in occ.values())


# ---- 指标与冲突 -----------------------------------------------------------


def _length_of(infos, assign, dirs, layer_depth):
    total = 0
    for k, info in enumerate(infos):
        L = assign[k]
        if L is None:
            continue
        total += info["span"] + layer_depth[L]
        if info["a"] != info["b"] and dirs.get(info["cid"], "R") != _toward(info):
            total += 0.5
    return total


def _crossing_pairs(infos, inv_pairs, assign):
    return [(infos[i]["cid"], infos[j]["cid"], assign[i])
            for i, j in inv_pairs
            if assign[i] is not None and assign[i] == assign[j]]


def _unavoidable(pair, infos, idx_of, assign, cap_of):
    """残留交叉是否无法避免：任一跳线改到别的可用槽层（容量允许）都不能减少交叉。"""
    used = {}
    for k, info in enumerate(infos):
        L = assign[k]
        if L is not None:
            for z in info["zones"]:
                used[(z, L)] = used.get((z, L), 0) + 1
    partners = {}
    for i in range(len(infos)):
        for j in range(len(infos)):
            if i != j and assign[i] is not None and assign[i] == assign[j] \
                    and (infos[i]["a"] - infos[j]["a"]) * (infos[i]["b"] - infos[j]["b"]) < 0:
                partners.setdefault(infos[i]["cid"], set()).add(infos[j]["cid"])
    for cid in pair:
        k = idx_of[cid]
        info = infos[k]
        cur = assign[k]
        cur_cross = sum(1 for p in partners.get(cid, ()))
        for L2 in info["layers"]:
            if L2 == cur:
                continue
            if any(used.get((z, L2), 0) >= cap_of[(z, L2)] for z in info["zones"]):
                continue
            new_cross = sum(1 for q, qq in enumerate(infos)
                            if assign[q] == L2
                            and (info["a"] - qq["a"]) * (info["b"] - qq["b"]) < 0)
            if new_cross < cur_cross:
                return False
    return True


# ---- 主入口 ---------------------------------------------------------------


def solve_layout(config, layout_raw):
    """先按 config 完成既有规划，再对规划出的跳线做走线排版。

    信号拓扑、常态模式与跳线端点完全由 plan_all 决定，本函数只分配
    槽层与绕行方向，不改动任何路由结论。
    """
    result, _plan = plan_all(config)         # ConfigError 直接上抛
    pair_summaries = result["pairs"]
    zones, col_of, layer_depth, cap_of = parse_layout(
        layout_raw, [p["id"] for p in pair_summaries])

    cables = result["cables"]
    infos = _cable_infos(cables, col_of, zones, layer_depth)
    idx_of = {info["cid"]: k for k, info in enumerate(infos)}
    inv_pairs = _inversions(infos)
    cross_zone_count = sum(1 for i in infos if i["fromZone"] != i["toZone"])

    note = None
    if infos:
        assign, exhausted = _optimize_assign(infos, cap_of, layer_depth, inv_pairs)
        if not exhausted:
            note = "搜索规模超出上限，已采用当前最佳方案（可能非全局最优）"
    else:
        assign = []
        note = "规划中未安装跳线，无需走线排版"

    # 优化方向：按层分组求解；基线方向取各跳线朝目标侧的自然方向（不统一协调）
    dirs = {}
    by_layer = {}
    for k, info in enumerate(infos):
        if assign[k] is not None:
            by_layer.setdefault(assign[k], []).append(info)
    for group in by_layer.values():
        dirs.update(_assign_directions(group))
    base_dirs = {info["cid"]: _toward(info) for info in infos}

    base_assign = _greedy_assign(infos, cap_of)
    base_cross = _crossing_pairs(infos, inv_pairs, base_assign)
    opt_cross = _crossing_pairs(infos, inv_pairs, assign)

    metrics = {
        "objective": ["交叉数最少", "跨区线数最少", "同层长度最短"],
        "optimized": {
            "crossings": len(opt_cross),
            "crossZone": cross_zone_count,
            "length": round(_length_of(infos, assign, dirs, layer_depth), 2),
            "gapConflicts": _gap_conflicts(infos, assign, dirs),
            "unassigned": sum(1 for a in assign if a is None),
        },
        "baseline": {
            "crossings": len(base_cross),
            "crossZone": cross_zone_count,
            "length": round(_length_of(infos, base_assign, base_dirs, layer_depth), 2),
            "gapConflicts": _gap_conflicts(infos, base_assign, base_dirs),
            "unassigned": sum(1 for a in base_assign if a is None),
        },
    }

    # 每根跳线的排版结论
    cables_out, conflicts = [], []
    for k, info in enumerate(infos):
        L = assign[k]
        d = dirs.get(info["cid"])
        entry = {
            "id": info["cid"],
            "fromJack": info["fromJack"], "toJack": info["toJack"],
            "fromPort": info["fromPort"], "toPort": info["toPort"],
            "fromCol": info["a"], "toCol": info["b"],
            "fromZone": zones[info["fromZone"]]["name"],
            "toZone": zones[info["toZone"]]["name"],
            "crossZone": info["fromZone"] != info["toZone"],
            "span": info["span"],
            "layer": L,
            "direction": d,
            "directionLabel": DIRECTION_LABELS.get(d) if d else None,
            "length": (round(info["span"] + layer_depth[L]
                             + (0.5 if info["a"] != info["b"] and d != _toward(info) else 0), 2)
                       if L is not None else None),
            "status": "routed" if L is not None else "unassigned",
        }
        if L is None:
            entry["reason"] = "跨度经过的分区没有容量剩余的公共走线槽层"
            conflicts.append({
                "type": "unassigned", "cables": [info["cid"]],
                "reason": f"跳线 #{info['cid']}（{info['fromJack']} → {info['toJack']}）"
                          f"无可用的公共走线槽层（槽层缺失或容量已满），无法分配",
            })
        cables_out.append(entry)

    crossing_out = [{"cables": [ci, cj], "layer": L,
                     "unavoidable": _unavoidable((ci, cj), infos, idx_of, assign, cap_of)}
                    for ci, cj, L in opt_cross]
    for cp in crossing_out:
        ci, cj = cp["cables"]
        conflicts.append({
            "type": "crossing", "cables": [ci, cj], "layer": cp["layer"],
            "unavoidable": cp["unavoidable"],
            "reason": f"跳线 #{ci} 与 #{cj} 在槽层 {cp['layer']} 同层交叉"
                      + ("；受公共槽层与容量限制，改层无法减少交叉，属于无法避免的冲突"
                         if cp["unavoidable"] else ""),
        })

    # 面板几何（供前端绘图）
    pair_by_id = {p["id"]: p for p in pair_summaries}
    jacks = []
    for z in zones:
        for pid in z["columns"]:
            p = pair_by_id[pid]
            jacks.append({
                "pair": pid, "col": col_of[pid],
                "zone": z["index"], "zoneName": z["name"],
                "topJack": p["topJack"], "topPort": p["topPort"],
                "bottomJack": p["bottomJack"], "bottomPort": p["bottomPort"],
                "modeLabel": p["modeLabel"], "normalActive": p["normalActive"],
            })
    used = {}
    for k, info in enumerate(infos):
        if assign[k] is not None:
            for z in info["zones"]:
                used[(z, assign[k])] = used.get((z, assign[k]), 0) + 1
    zones_out = [{
        "index": z["index"], "name": z["name"], "range": z["range"],
        "columns": [{"pair": pid, "col": col_of[pid]} for pid in z["columns"]],
        "layers": [{"id": l["id"], "depth": layer_depth[l["id"]],
                    "capacity": l["capacity"],
                    "used": used.get((z["index"], l["id"]), 0)}
                   for l in z["layers"]],
    } for z in zones]

    return {
        "ok": not conflicts,
        "planOk": result["ok"],
        "solverNote": note,
        "zones": zones_out,
        "jacks": jacks,
        "cables": cables_out,
        "crossingPairs": crossing_out,
        "conflicts": conflicts,
        "metrics": metrics,
    }
