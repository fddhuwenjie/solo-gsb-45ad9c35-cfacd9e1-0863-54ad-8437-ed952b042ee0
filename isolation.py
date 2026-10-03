"""串音隔离规划（crosstalk isolation planning）。

用户为每条路由指定一个**隔离组**，并声明若干**禁配组对**（两组信号不得
互相连通）。系统在全常态 / 半常态 / 直通语义下对**全部路由重新规划**：

  - 全常态 full：上 / 下插孔任一侧插入插头即断开常态；
  - 半常态 half：仅下插孔插入断开常态，上插孔插入时常态保持、信号并送；
  - 直通 through：上下插孔之间没有常态连接。

隔离判据建立在规划完成后的**无向物理连通分量**上（与命名审计同一张物理
图）：边包括 ① 设备内部连接（输入→输出，永在）、② 生效常态、③ 已安装
前面板跳线。禁配组各自的路由链路端口不得落入同一个物理连通分量——
半常态上插孔插线的“并送”汇合也在其中。

搜索策略（确定性）：

  1. 先按**输入顺序贪心**（与 ``solver.plan_all`` 相同的逐条最短路）评估，
     记录其是否满足隔离（结果只作对照与冲突时的拓扑展示）；
  2. 正式搜索在“下一条规划哪条路由 × 该路由走哪条候选路径”两层上做带回溯
     的深度优先搜索：路由顺序按输入序号优先尝试（贪心顺序是第一条分支），
     候选路径按跳线数升序枚举（有界环无关路径枚举），故贪心不满足隔离时会
     **回溯重排已规划路由**，也会为后续路由改走多用跳线的替代路径；
  3. 目标按字典序：**全部路由可通 → 跳线总数最少 → 规划顺序尽量贴近输入
     顺序**（迭代加深：预算 0,1,2,… 依次搜索）；
  4. 剪枝：
       - 设备内部边与已安装跳线只会增加、不会消失（跳线只插不拆），在这两类
         边构成的“永久图”上一旦禁配组已共分量，该分支直接剪除；
       - 任一条剩余路由在当前状态下已无任何通路（占用与常态保护单调增长），
         分支剪除；
       - 剩余路由各自最短跳线数之和的下界超过当前预算，剪除；
  5. 全部规划完成后仍在最终物理图上做一次完整隔离校验，并保留**反馈环路
     检测**（``solver.detect_loops``，仅警告、不使方案失效）。

无解（或搜索超限时不伪称无解）时抛 :class:`IsolationConflict`（409），
携带：冲突组对、组对之间在物理图上的**多源 BFS 最短共享证据**（设备内部 /
常态 / 跳线逐跳）、未满足路由及原因、贪心对照拓扑——**不返回任何接线步骤，
不提交部分方案**。
"""

from collections import defaultdict, deque

from solver import (FULL, MODE_LABELS, THROUGH, ConfigError, Solver,
                    detect_loops, diagnose, pairs_summary, parse_config,
                    plug_note, _pid)

PLAN_NODE_CAP = 120_000        # 回溯搜索节点上限（超出返回 inconclusive）
PATH_ENUM_CAP = 40             # 每条路由在每个状态下保留的候选路径上限
PATH_VISIT_CAP = 6000          # 单次路径枚举的 DFS 访问上限
MAX_CABLE_DEPTH = 4            # 单条路由候选路径最多经过的跳线数


class IsolationError(ConfigError):
    """隔离声明非法（400 ISOLATION_INVALID）。"""

    def __init__(self, message, details=None):
        super().__init__(message, code="ISOLATION_INVALID",
                         details=details or {})


class IsolationConflict(ConfigError):
    """不存在满足隔离约束的完整规划（409 ISOLATION_CONFLICT）。"""

    def __init__(self, message, payload):
        super().__init__(message, code="ISOLATION_CONFLICT", details=payload)
        self.payload = payload


# ---- 隔离声明解析 ---------------------------------------------------------


def parse_isolation(raw, n_routes):
    """校验隔离声明，返回 (groups, forbidden)。

    groups:    与路由等长的列表，每项为组名字符串或 None（不分组）；
    forbidden: 排序去重后的 (a, b) 元组列表。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise IsolationError(
            'isolation 必须是 JSON 对象，形如 {"groups": [...], '
            '"forbiddenPairs": [["A", "B"]]}')
    grow = raw.get("groups", [])
    if not isinstance(grow, list):
        raise IsolationError("isolation.groups 必须是数组，按路由顺序填写隔离组名")
    if len(grow) != n_routes:
        raise IsolationError(
            f"隔离组数量（{len(grow)}）必须与路由数量（{n_routes}）一致",
            details={"expected": n_routes, "actual": len(grow)})
    groups = []
    for i, g in enumerate(grow):
        if g is None:
            groups.append(None)
            continue
        if not isinstance(g, str):
            raise IsolationError(f"路由 {i + 1} 的隔离组必须是字符串名称（留空表示不分组）")
        t = g.strip()
        if len(t) > 32:
            raise IsolationError(f"路由 {i + 1} 的隔离组名不能超过 32 个字符")
        groups.append(t or None)
    used = {g for g in groups if g}

    fp_raw = raw.get("forbiddenPairs", [])
    if not isinstance(fp_raw, list):
        raise IsolationError("isolation.forbiddenPairs 必须是 [组A, 组B] 形式的数组")
    forbidden, seen = [], set()
    for i, pair in enumerate(fp_raw):
        if not isinstance(pair, (list, tuple)) or len(pair) != 2:
            raise IsolationError(f"forbiddenPairs[{i}] 必须是恰好两个组名的数组")
        a, b = pair
        if not isinstance(a, str) or not isinstance(b, str):
            raise IsolationError(f"forbiddenPairs[{i}] 的两个组都必须是字符串名称")
        a, b = a.strip(), b.strip()
        if not a or not b:
            raise IsolationError(f"forbiddenPairs[{i}] 组名不能为空")
        if a == b:
            raise IsolationError(f"forbiddenPairs[{i}] 不能声明组 “{a}” 与自身禁配")
        if a not in used or b not in used:
            raise IsolationError(
                f"forbiddenPairs[{i}]：隔离组 “{a if a not in used else b}” "
                "没有被任何路由使用，无法声明禁配",
                details={"unknown": sorted({a, b} - used)})
        key = tuple(sorted((a, b)))
        if key not in seen:
            seen.add(key)
            forbidden.append(key)
    forbidden.sort()
    return groups, forbidden


# ---- 搜索状态 -------------------------------------------------------------


class Node:
    """一次规划前缀的完整物理状态（全程不可变，分支时整体复制）。"""

    __slots__ = ("plugs", "cables", "protected", "next_cid", "commits", "order")

    def __init__(self, plugs=None, cables=None, protected=None,
                 next_cid=1, commits=None, order=None):
        self.plugs = plugs or {}                # jack -> 跳线编号
        self.cables = cables or []              # [{id, fromJack, toJack, fromPort, toPort}]
        self.protected = protected or set()     # 已提交路由占用的常态（插孔对编号）
        self.next_cid = next_cid
        # commits: {routeIdx: (items, {(fj,tj): cid}, normals(set))}
        self.commits = commits if commits is not None else {}
        self.order = order or []                # 已提交路由序号（规划顺序）

    @property
    def cable_count(self):
        return len(self.cables)


def bind(solver, node):
    """把搜索状态挂到 Solver 上以复用其常态判定 / 最短路 / 邻居枚举。"""
    solver.plugs = node.plugs
    solver.cables = node.cables
    solver.protected_normals = node.protected
    solver.next_cid = node.next_cid


def commit(solver, node, items):
    """按 Solver.commit 相同语义提交一条路径，返回 (新 Node, 跳线映射)。"""
    plugs = dict(node.plugs)
    cables = [dict(c) for c in node.cables]
    protected = set(node.protected)
    cid_map, added = {}, 0
    for kind, payload in items:
        if kind != "edge":
            continue
        if payload[0] == "normal":
            protected.add(payload[1]["id"])
        elif payload[0] == "cable":
            _, fj, tj = payload
            if fj not in plugs:
                cid = node.next_cid + added
                added += 1
                cables.append({
                    "id": cid, "fromJack": fj, "toJack": tj,
                    "fromPort": solver.jack_port[fj],
                    "toPort": solver.jack_port[tj],
                })
                plugs[fj] = cid
                plugs[tj] = cid
            cid_map[(fj, tj)] = plugs[fj]
    new = Node(plugs, cables, protected, node.next_cid + added,
               dict(node.commits), list(node.order))
    return new, cid_map


def attach_commit(node, idx, items, cid_map, normals):
    new = Node(node.plugs, node.cables, node.protected, node.next_cid,
               dict(node.commits), node.order + [idx])
    new.commits[idx] = (items, cid_map, normals)
    return new


# ---- 候选路径枚举 ---------------------------------------------------------


def _edge_key(edge):
    if edge[0] == "normal":
        return ("n", edge[1]["id"])
    if edge[0] == "cable":
        return ("c", edge[1], edge[2])
    return ("d", edge[1])


def enumerate_paths(solver, src, dst, max_cables,
                    cap=PATH_ENUM_CAP, visit_cap=PATH_VISIT_CAP):
    """枚举当前插头状态下 src→dst 的环无关路径，按跳线数升序。

    返回 (paths, truncated)：paths 为 [(items, cable_count), ...]，已排序；
    truncated 表示枚举触及访问 / 条数上限（可能遗漏候选）。
    邻居枚举与 ``Solver._neighbors`` 完全一致（含插孔占用与常态保护限制），
    路径另需通过 ``Solver.path_consistent``（自身插头不得掐断自身常态）。
    """
    found = {}
    visits = [0]
    truncated = [False]

    def dfs(u, items, keys, cnum, visited):
        if u == dst:
            found[tuple(keys)] = (cnum, list(items))
            return
        if visits[0] >= visit_cap or len(found) >= cap * 4:
            truncated[0] = True
            return
        visits[0] += 1
        cands = sorted(solver._neighbors(u, False),
                       key=lambda e: (e[1],) + _edge_key(e[2]))
        for v, _w, edge in cands:
            if v in visited:
                continue
            ec = 1 if edge[0] == "cable" else 0
            if cnum + ec > max_cables:
                continue
            visited.add(v)
            items.append(("edge", edge))
            items.append(("port", v))
            keys.append(_edge_key(edge))
            dfs(v, items, keys, cnum + ec, visited)
            keys.pop()
            items.pop()
            items.pop()
            visited.discard(v)

    dfs(src, [("port", src)], [], 0, {src})
    paths = []
    for keys, (cnum, items) in found.items():
        if solver.path_consistent(items):
            paths.append((items, cnum, keys))
    paths.sort(key=lambda p: (p[1], len(p[0]), p[2]))
    paths = paths[:cap]
    return [(items, cnum) for items, cnum, _ in paths], (truncated[0] or len(found) > cap)


# ---- 物理图 / 连通分量 ----------------------------------------------------


def active_normal_ids(solver, pairs, plugs):
    ids = set()
    for p in pairs:
        if p["mode"] == THROUGH or not (p["top"] and p["bottom"]):
            continue
        t, b = f"{p['id']}T", f"{p['id']}B"
        if p["mode"] == FULL:
            ok = t not in plugs and b not in plugs
        else:  # half：仅下插孔插线断常态（上插孔插线并送）
            ok = b not in plugs
        if ok:
            ids.add(p["id"])
    return ids


def _union_find(nodes):
    parent = {n: n for n in nodes}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    return find, union


def build_dsu(solver, devices, node, pairs, include_normals):
    """设备内部 + 已安装跳线（+ 生效常态）的无向并查集。"""
    find, union = _union_find(solver.port_dir)
    for name, d in devices.items():
        for i in d["inputs"]:
            for o in d["outputs"]:
                union(f"{name}.{i}", f"{name}.{o}")
    for c in node.cables:
        union(c["fromPort"], c["toPort"])
    if include_normals:
        for nid in active_normal_ids(solver, pairs, node.plugs):
            p = solver.pair_of_jack[f"{nid}T"]
            union(_pid(p["top"]), _pid(p["bottom"]))
    return find


def device_internal_components(devices):
    """设备内部边（与规划无关）的连通分量：{port: 不可变分量键}。

    单设备内全部 in/out 端口经设备内部互连，直接用 frozenset 端口集作键；
    不同设备的内部分量互不相交。
    """
    comp_of = {}
    for name, d in devices.items():
        ports = [f"{name}.{i}" for i in d["inputs"]] + \
                [f"{name}.{o}" for o in d["outputs"]]
        if ports:
            key = frozenset(ports)
            for p in ports:
                comp_of[p] = key
    return comp_of


def fast_dsu(node, base_comp, extra_edges=()):
    """以预计算的设备内部不可变分量为基线，只合并跳线与额外边。

    设备内部 in→out 全互连且永不变化，故每个搜索节点无需重建这些边；
    本并查集的“原子”为内部分量键（frozenset）与未接入设备的单端口。
    extra_edges: [(a_port, b_port), ...]（如生效常态或理想路径边）。
    """
    parent = {}

    def key_of(port):
        return base_comp.get(port, port)

    def intern(port):
        k = key_of(port)
        if k not in parent:
            parent[k] = k
        return k

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k

    def union(a, b):
        ra, rb = find(intern(a)), find(intern(b))
        if ra != rb:
            parent[rb] = ra

    for c in node.cables:
        union(c["fromPort"], c["toPort"])
    for a, b in extra_edges:
        union(a, b)

    def find_port(port):
        k = key_of(port)
        if k not in parent:
            return k
        return find(k)

    return find_port


def chain_ports(commits):
    """已提交路由链路占用的全部端口 {routeIdx: [ports...]}。"""
    out = {}
    for idx, (items, _cid_map, _normals) in commits.items():
        out[idx] = [payload for kind, payload in items if kind == "port"]
    return out


def group_roots(find, groups, ports_of_route):
    """隔离组 -> 其所有路由链路触及的并查集根集合。"""
    roots = defaultdict(set)
    for idx, ports in ports_of_route.items():
        g = groups[idx]
        if g:
            roots[g].update(find(p) for p in ports)
    return roots


def violated_pairs(find, groups, forbidden, ports_of_route):
    """返回当前状态下发生共分量的禁配组对列表（含各组根集合）。"""
    roots = group_roots(find, groups, ports_of_route)
    bad = []
    for a, b in forbidden:
        if roots.get(a, set()) & roots.get(b, set()):
            bad.append((a, b))
    return bad


def normal_edges_of(solver, pairs, plugs):
    """当前插头状态下生效常态的无向边 [(top_port, bottom_port)]。"""
    edges = []
    for nid in active_normal_ids(solver, pairs, plugs):
        p = solver.pair_of_jack[f"{nid}T"]
        edges.append((_pid(p["top"]), _pid(p["bottom"])))
    return edges


def fast_violated(node, base_comp, groups, forbidden, ports_of_route,
                  extra_edges=()):
    """等价于 violated_pairs(build_dsu(...))，但跳过设备内部边重建。"""
    find = fast_dsu(node, base_comp, extra_edges)
    roots = group_roots(find, groups, ports_of_route)
    return [(a, b) for a, b in forbidden
            if roots.get(a, set()) & roots.get(b, set())]


# ---- 证据路径 -------------------------------------------------------------


def build_adjacency(solver, devices, pairs, node, include_normals):
    """无向物理图 {port: [(neighbor, edge), ...]}，edge 为序列化友好的描述。"""
    adj = defaultdict(list)

    def link(a, b, edge):
        adj[a].append((b, edge))
        adj[b].append((a, edge))

    for name, d in devices.items():
        for i in d["inputs"]:
            for o in d["outputs"]:
                link(f"{name}.{i}", f"{name}.{o}",
                     {"type": "device", "device": name,
                      "label": f"{name} 内部"})
    if include_normals:
        for nid in active_normal_ids(solver, pairs, node.plugs):
            p = solver.pair_of_jack[f"{nid}T"]
            link(_pid(p["top"]), _pid(p["bottom"]),
                 {"type": "normal", "pairId": nid,
                  "label": f"{MODE_LABELS[p['mode']]} · 第{nid}对"})
    for c in node.cables:
        link(c["fromPort"], c["toPort"],
             {"type": "cable", "cableId": c["id"], "label": f"跳线 #{c['id']}"})
    for port in solver.port_dir:
        adj.setdefault(port, [])
    return adj


def build_augmented_adjacency(solver, devices, pairs, node, extra_items):
    """终止状态物理图 + 未提交路由“理想路径”（忽略占用）的边。

    穷尽回溯后仍未提交的路由，其理想路径给出的是它**若要通**在任何规划里
    都必须使用的资源（设备内部 / 常态 / 插孔跳线）。把这些边与终止状态的
    实际物理边合并后再做无向连通归并，即可恢复“要让两组路由同时成立就必然
    共分量”的禁配组对——包括共享背板插孔（两根跳线争用同一 T 插孔）这类
    在终止节点尚未物理成型、但已被穷尽搜索证明无解的冲突。
    """
    adj = build_adjacency(solver, devices, pairs, node, True)
    extra_normals, extra_cable_pairs = set(), []
    for items in extra_items:
        if not items:
            continue
        ns, cps = Solver.extract_usage(items)
        extra_normals |= ns
        extra_cable_pairs.extend(cps)

    def link(a, b, edge):
        adj[a].append((b, edge))
        adj[b].append((a, edge))

    live_ids = active_normal_ids(solver, pairs, node.plugs)
    for nid in sorted(extra_normals - live_ids):
        p = solver.pair_of_jack[f"{nid}T"]
        if p["top"] and p["bottom"]:
            link(_pid(p["top"]), _pid(p["bottom"]),
                 {"type": "normal", "pairId": nid,
                  "label": f"{MODE_LABELS[p['mode']]} · 第{nid}对（理想路径需启用）"})
    next_vid = max((c["id"] for c in node.cables), default=0) + 1
    for fj, tj in extra_cable_pairs:
        link(solver.jack_port[fj], solver.jack_port[tj],
             {"type": "cable", "cableId": next_vid,
              "label": f"跳线候选 {fj} → {tj}（共享背板插孔）"})
        next_vid += 1
    return adj


def adjacency_dsu(adj, nodes):
    find, union = _union_find(nodes)
    for u, nbrs in adj.items():
        for v, _ in nbrs:
            union(u, v)
    return find


def shortest_between(adj, starts, goals):
    """多源 BFS：starts 任一端点到 goals 任一端点的最短无向路径（端口列表）。"""
    starts, goals = set(starts), set(goals)
    common = starts & goals
    if common:
        return [min(common)]
    prev = {s: (None, None) for s in sorted(starts)}
    q = deque(sorted(starts))
    hit = None
    while q and hit is None:
        u = q.popleft()
        for v, edge in adj.get(u, ()):
            if v in prev:
                continue
            prev[v] = (u, edge)
            if v in goals:
                hit = v
                break
            q.append(v)
    if hit is None:
        return None
    nodes, x = [], hit
    while x is not None:
        nodes.append(x)
        x, _ = prev[x]
    nodes.reverse()
    return nodes


def edge_between(adj, a, b):
    for v, edge in adj.get(a, ()):
        if v == b:
            return edge
    return None


def evidence_items(nodes, adj, solver, groups_at_port):
    items = []
    for i, node in enumerate(nodes):
        items.append({
            "kind": "port", "port": node,
            "jack": solver.port_jack.get(node),
            "groups": sorted(groups_at_port.get(node, ())),
        })
        if i + 1 < len(nodes):
            edge = edge_between(adj, node, nodes[i + 1]) or \
                {"type": "link", "label": "连通"}
            items.append({"kind": "edge", **edge})
    return items


def groups_on_ports(groups, ports_of_route):
    m = defaultdict(set)
    for idx, ports in ports_of_route.items():
        g = groups[idx]
        if g:
            for p in ports:
                m[p].add(g)
    return m


def pair_evidence(solver, devices, pairs, node, groups, commits, a, b,
                  adj_full=None, adj_perm=None):
    """组对 (a,b) 之间的最短共享证据（全图 / 永久图各一份）。"""
    ports_of_route = chain_ports(commits)
    gap = groups_on_ports(groups, ports_of_route)
    starts = {p for idx, ps in ports_of_route.items() if groups[idx] == a for p in ps}
    goals = {p for idx, ps in ports_of_route.items() if groups[idx] == b for p in ps}
    out = {}
    adj_full_cached = {}
    for kind, normals, override in (("full", True, adj_full),
                                    ("permanent", False, adj_perm)):
        if override is not None:
            adj = override
        else:
            adj = adj_full_cached.get(normals) or build_adjacency(
                solver, devices, pairs, node, normals)
            adj_full_cached[normals] = adj
        nodes = shortest_between(adj, starts, goals)
        if nodes:
            out[kind] = evidence_items(nodes, adj, solver, gap)
    return out


# ---- 贪心对照（输入顺序） -------------------------------------------------


def greedy_baseline(solver, routes):
    """与 plan_all 相同的输入顺序逐条最短路贪心。返回 (node, failed)。

    Solver 自身状态随每条提交更新，因此直接使用 solver.commit；最后把
    Solver 状态与搜索 Node 对齐，返回的 node 绑定的就是最终物理状态。
    """
    node = Node()
    bind(solver, node)
    failed = []
    for idx, r in enumerate(routes):
        src, dst = _pid(r["source"]), _pid(r["target"])
        found = solver.find_path(src, dst)
        if found and not solver.path_consistent(found[1]):
            found = None
        if not found:
            ideal = solver.find_path(src, dst, ignore_plugs=True)
            reasons, _ports = diagnose(solver, src, dst, ideal, set())
            failed.append({"index": idx, "source": src, "target": dst,
                           "reasons": reasons})
            break
        _cost, items = found
        cid_map = solver.commit(items)
        normals, _cable_pairs = Solver.extract_usage(items)
        # 让搜索 Node 与刚被 solver.commit 改写的状态保持一致
        node = Node(dict(solver.plugs), [dict(c) for c in solver.cables],
                    set(solver.protected_normals), solver.next_cid,
                    dict(node.commits), node.order + [idx])
        node.commits[idx] = (items, cid_map, normals)
    return node, failed


# ---- 主搜索 ---------------------------------------------------------------


def plan_isolation(config, iso_raw):
    """按隔离约束重新规划全部路由。成功返回结果 dict；失败抛 IsolationConflict。"""
    devices, pairs, routes, port_jack = parse_config(config)
    groups, forbidden = parse_isolation(iso_raw, len(routes))
    solver = Solver(devices, pairs, routes, port_jack)

    # ---- 输入顺序贪心对照（也用作无解时的冲突拓扑展示） ------------------
    greedy_node, greedy_failed = greedy_baseline(solver, routes)
    bind(solver, greedy_node)
    g_ports = chain_ports(greedy_node.commits)
    g_find_full = build_dsu(solver, devices, greedy_node, pairs, True)
    g_find_perm = build_dsu(solver, devices, greedy_node, pairs, False)
    g_viol = violated_pairs(g_find_full, groups, forbidden, g_ports)
    g_viol_perm = violated_pairs(g_find_perm, groups, forbidden, g_ports)
    greedy_meta = {
        "ok": not greedy_failed and not g_viol,
        "cableCount": greedy_node.cable_count,
        "routedCount": len(greedy_node.order),
        "violatedPairs": [list(p) for p in g_viol],
        "permanentViolatedPairs": [list(p) for p in g_viol_perm],
        "unroutable": greedy_failed,
        "backtrackNeeded": bool(greedy_failed or g_viol),
    }

    # ---- 回溯搜索 --------------------------------------------------------
    base_comp = device_internal_components(devices)
    rinfo = [(i, _pid(r["source"]), _pid(r["target"])) for i, r in enumerate(routes)]
    all_idx = set(range(len(routes)))
    stats = {"nodes": 0, "truncated": False, "deepRoutes": set()}
    best_terminal = {"score": None, "node": None, "violations": None,
                     "complete": False, "seq": 0}
    seq = [0]
    short_memo, paths_memo = {}, {}

    def state_key(node):
        return (frozenset(node.plugs), frozenset(node.protected))

    def shortest_cost(node, idx):
        """当前状态下某条路由的最短跳线数；无通路返回 None（结果带记忆）。"""
        key = (*state_key(node), idx)
        if key not in short_memo:
            bind(solver, node)
            _i, src, dst = rinfo[idx]
            found = solver.find_path(src, dst)
            if found and not solver.path_consistent(found[1]):
                found = None
            short_memo[key] = None if found is None else found[0]
        return short_memo[key]

    def candidates(node, idx, max_cables):
        key = (*state_key(node), idx, max_cables)
        if key not in paths_memo:
            bind(solver, node)
            _i, src, dst = rinfo[idx]
            paths, trunc = enumerate_paths(solver, src, dst, max_cables)
            if trunc:
                stats["truncated"] = True
            paths_memo[key] = paths
        return paths_memo[key]

    def record_terminal(node, violations, complete):
        score = (0 if complete else 1, len(violations), node.cable_count,
                 -len(node.order), seq[0])
        seq[0] += 1
        if best_terminal["score"] is None or score < best_terminal["score"]:
            best_terminal.update(score=score, node=node, violations=violations,
                                 complete=complete, seq=seq[0])

    abort = [False]

    def dfs(node, budget):
        if abort[0]:
            return None
        stats["nodes"] += 1
        if stats["nodes"] > PLAN_NODE_CAP:
            abort[0] = True
            return None
        bind(solver, node)
        ports_of_route = chain_ports(node.commits)

        # 永久图（设备内部 + 跳线）上的共分量：未来只可能断线、不可能拆开
        perm_bad = fast_violated(node, base_comp, groups, forbidden, ports_of_route)
        if perm_bad:
            record_terminal(node, perm_bad, False)
            return None

        remaining = sorted(all_idx - set(node.order))
        if not remaining:
            full_bad = fast_violated(node, base_comp, groups, forbidden,
                                     ports_of_route,
                                     normal_edges_of(solver, pairs, node.plugs))
            if not full_bad:
                return node
            record_terminal(node, full_bad, True)
            return None

        # 剩余路由可通性：占用与常态保护单调增长，当前不可通则永不可通。
        # 注意不能用“各路由最短跳线数之和”做预算下界——多条后续路由可以
        # 复用同一根已安装跳线（与输入顺序无关），求和会高估、误剪可行分支。
        for idx in remaining:
            if shortest_cost(node, idx) is None:
                record_terminal(node, perm_bad, False)
                return None
            if shortest_cost(node, idx) > MAX_CABLE_DEPTH:
                stats["deepRoutes"].add(idx)

        used = node.cable_count
        for nxt in remaining:                 # 输入序号优先 = 贪心顺序排第一
            room = budget - used
            depth = min(MAX_CABLE_DEPTH, room)
            if shortest_cost(node, nxt) is None:
                continue
            for items, cnum in candidates(node, nxt, depth):
                if cnum > room:
                    continue
                child, cid_map = commit(solver, node, items)
                normals, _pairs2 = Solver.extract_usage(items)
                child = attach_commit(child, nxt, items, cid_map, normals)
                found = dfs(child, budget)
                if found is not None or abort[0]:
                    return found
        return None

    feasible = None
    max_budget = len(pairs)                  # 每根跳线至少占用一对不同的 T/B
    for budget in range(0, max_budget + 1):
        feasible = dfs(Node(), budget)
        if feasible is not None or abort[0]:
            break

    note = None
    inconclusive = abort[0]
    if abort[0]:
        note = (f"回溯搜索超过 {PLAN_NODE_CAP} 个节点上限：已列出搜索中发现的"
                "隔离冲突，但未能在时限内证明不存在可行规划（inconclusive）")
    elif stats["deepRoutes"]:
        inconclusive = True
        note = ("部分路由的最短通路需要超过 "
                f"{MAX_CABLE_DEPTH} 条跳线：超出候选路径枚举深度，结果可能非最优"
                "或存在未检出的可行规划（inconclusive）")

    if feasible is None:
        raise IsolationConflict(
            "不存在满足全部串音隔离约束的完整路由规划：已穷尽回溯重排与替代"
            "路径，仍有禁配组无法避免共享物理连通分量（或存在无法路由的请求）",
            _conflict_payload(solver, devices, pairs, routes, groups, forbidden,
                              best_terminal, greedy_node, greedy_meta,
                              stats, inconclusive, note))

    return _success_payload(solver, devices, pairs, routes, groups, forbidden,
                            feasible, greedy_meta, stats, note)


# ---- 结果序列化 -----------------------------------------------------------


def _components(solver, devices, pairs, groups, node, commits, forbidden):
    """最终/终止物理图的分量摘要（供前端按隔离组着色拓扑）。"""
    find = build_dsu(solver, devices, node, pairs, True)
    ports_of_route = chain_ports(commits)
    touched_roots = {find(p) for ps in ports_of_route.values() for p in ps}
    comp_ports = defaultdict(list)
    for p in solver.port_dir:
        r = find(p)
        if r in touched_roots:
            comp_ports[r].append(p)
    root_cid = {r: i for i, r in enumerate(sorted(comp_ports))}
    groups_here = defaultdict(set)
    routes_here = defaultdict(set)
    for idx, ports in ports_of_route.items():
        if not ports:
            continue
        cid = root_cid[find(ports[0])]
        routes_here[cid].add(idx)
        g = groups[idx]
        if g:
            groups_here[cid].add(g)
    fset = [set(p) for p in forbidden]
    out = []
    for r in sorted(comp_ports):
        cid = root_cid[r]
        gs = sorted(groups_here.get(cid, ()))
        violated = any({a, b} <= set(gs) for a, b in fset)
        out.append({
            "id": cid,
            "groups": gs,
            "routes": sorted(routes_here.get(cid, ())),
            "violated": violated,
            "ports": [{"port": p, "jack": solver.port_jack.get(p)}
                      for p in sorted(comp_ports[r])],
        })
    return out, find, ports_of_route


def _success_payload(solver, devices, pairs, routes, groups, forbidden,
                     node, greedy_meta, stats, note):
    bind(solver, node)
    components, find, ports_of_route = _components(
        solver, devices, pairs, groups, node, node.commits, forbidden)
    comp_of_root = {}
    for c in components:
        for p in c["ports"]:
            comp_of_root[find(p["port"])] = c["id"]

    routes_out = []
    used_normals = set()
    for idx, r in enumerate(routes):
        src, dst = _pid(r["source"]), _pid(r["target"])
        items, cid_map, normals = node.commits[idx]
        used_normals |= normals
        used_ids = sorted(set(cid_map.values()))
        chain = solver.build_chain(items, cid_map)
        routes_out.append({
            "index": idx, "source": src, "target": dst, "status": "ok",
            "group": groups[idx], "cables": used_ids,
            "normalIds": sorted(normals),
            "chain": chain,
            "component": comp_of_root[find(src)],
            "planningStep": node.order.index(idx) + 1,
        })

    loops = detect_loops(devices, pairs, node.cables, solver.normal_active)
    notes = []
    for c in node.cables:
        for jack in (c["fromJack"], c["toJack"]):
            n = plug_note(solver.pair_of_jack[jack], jack, solver.jack_port)
            if n and n not in notes:
                notes.append(n)
    steps = [
        f"第 {c['id']} 步：取跳线 #{c['id']}，一端插入前面板 {c['fromJack']}"
        f"（{c['fromPort']}），另一端插入 {c['toJack']}（{c['toPort']}）"
        for c in node.cables
    ]

    groups_out = []
    for name in sorted({g for g in groups if g}):
        idxs = [i for i, g in enumerate(groups) if g == name]
        cids = sorted({comp_of_root[find(p)]
                       for i in idxs for p in ports_of_route[i]})
        groups_out.append({"name": name, "routeIndexes": idxs,
                           "componentIds": cids})

    backtracked = node.order != list(range(len(routes)))
    return {
        "ok": True,
        "isolated": True,
        "cableCount": node.cable_count,
        "cables": [dict(c) for c in node.cables],
        "steps": steps,
        "notes": notes,
        "routes": routes_out,
        "conflicts": [],
        "feedbackLoops": loops,
        "pairs": pairs_summary(pairs, solver),
        "components": components,
        "groups": groups_out,
        "forbiddenPairs": [{"a": a, "b": b, "satisfied": True} for a, b in forbidden],
        "isolation": {
            "greedyOk": greedy_meta["ok"],
            "greedyCableCount": greedy_meta["cableCount"],
            "backtracked": backtracked,
            "routeOrder": list(node.order),
            "searchNodes": stats["nodes"],
            "truncated": stats["truncated"],
            "solverNote": note,
        },
    }


def _conflict_payload(solver, devices, pairs, routes, groups, forbidden,
                      best, greedy_node, greedy_meta, stats, inconclusive, note):
    """无解 / 超限时的 409 结构化负载：冲突组对 + 最短证据 + 未满足路由。

    证据一律在**终止节点自身的物理状态**上计算（不把其他剪枝节点上的违例
    带到这里，否则证据与拓扑会对不上）。
    """
    node = best["node"]
    if node is None:
        node = Node()
    bind(solver, node)
    commits = node.commits
    ports_of_route = chain_ports(commits)
    find_full = build_dsu(solver, devices, node, pairs, True)
    find_perm = build_dsu(solver, devices, node, pairs, False)
    live_bad = set(map(tuple, violated_pairs(find_full, groups, forbidden,
                                             ports_of_route)))
    perm_bad_all = set(map(tuple, violated_pairs(find_perm, groups, forbidden,
                                                 ports_of_route)))

    # 终止节点上未提交路由的理想路径（忽略占用）：用于穷尽后恢复结构性冲突，
    # 也用于把这些路由的链路端口并入“若要通就必然触及”的归并
    committed = set(node.order)
    ideal_items = {}
    augmented_ports = {idx: list(ps) for idx, ps in ports_of_route.items()}
    for idx, r in enumerate(routes):
        if idx in committed:
            continue
        src, dst = _pid(r["source"]), _pid(r["target"])
        ideal = solver.find_path(src, dst, ignore_plugs=True)
        if ideal:
            ideal_items[idx] = ideal[1]
            augmented_ports[idx] = [p for k, p in ideal[1] if k == "port"]

    if not inconclusive:
        adj_aug = build_augmented_adjacency(
            solver, devices, pairs, node, list(ideal_items.values()))
        find_aug = adjacency_dsu(adj_aug, solver.port_dir)
        aug_roots = defaultdict(set)
        for idx, ps in augmented_ports.items():
            g = groups[idx]
            if g:
                aug_roots[g].update(find_aug(p) for p in ps)
        structural_bad = set()
        for a, b in forbidden:
            if aug_roots.get(a, set()) & aug_roots.get(b, set()):
                structural_bad.add((a, b))
    else:
        adj_aug, find_aug, structural_bad = None, None, set()

    def routes_of(g):
        return [i for i, x in enumerate(groups) if x == g]

    conflicts_out = []
    for a, b in sorted(live_bad | perm_bad_all | structural_bad):
        is_structural = (a, b) in structural_bad and (a, b) not in live_bad
        in_perm = (a, b) in perm_bad_all or is_structural
        ev = pair_evidence(
            solver, devices, pairs, node, groups,
            {**commits, **{i: (items, None, None)
                           for i, items in ideal_items.items()}},
            a, b, adj_full=adj_aug if is_structural else None)
        if is_structural:
            message = (
                f"隔离组 “{a}” 与 “{b}” 被声明为禁配；穷尽回溯重排已规划路由、"
                "并枚举各条路由的全部替代路径后，要让两组路由同时成立就必然"
                "共享同一物理连通分量（可能是共享设备内部连接，或两条跳线争用"
                "同一背板插孔——该插孔物理上只能插一根跳线），任何跳线数量与"
                "规划顺序都无法消除该共分量")
        else:
            message = (
                f"隔离组 “{a}” 与 “{b}” 被声明为禁配，但穷尽回溯重排与替代路径后，"
                "两组路由仍落入同一物理连通分量"
                + ("；共享路径只经过设备内部连接 / 已安装跳线（不会被任何插头操作"
                   "断开），无法通过加跳线或调整规划顺序消除" if in_perm else
                   "；当前终止状态下生效常态仍把两组连通"))
        conflicts_out.append({
            "groups": [a, b],
            "permanent": in_perm,
            "structural": is_structural,
            "message": message,
            "routesByGroup": {a: routes_of(a), b: routes_of(b)},
            "evidence": ev.get("full") or ev.get("permanent"),
            "permanentEvidence": ev.get("permanent"),
        })

    # 未满足路由：① 卷入禁配共分量的路由（含未提交、由扩展分析恢复的）；
    # ② 终止状态下确实无法与已规划路由共存的路由（附冲突原因）
    conflict_routes = {i for c in conflicts_out
                       for i in c["routesByGroup"][c["groups"][0]]
                       + c["routesByGroup"][c["groups"][1]]}
    unsatisfied = [{
        "index": i,
        "source": _pid(routes[i]["source"]),
        "target": _pid(routes[i]["target"]),
        "group": groups[i],
        "status": "isolation-conflict",
        "reasons": [f"所属隔离组 “{groups[i]}” 卷入无法消除的禁配共分量冲突"],
    } for i in sorted(conflict_routes)]
    for idx, r in enumerate(routes):
        if idx in committed or idx in conflict_routes:
            continue
        src, dst = _pid(r["source"]), _pid(r["target"])
        ideal = solver.find_path(src, dst, ignore_plugs=True)
        found = solver.find_path(src, dst)
        if found and not solver.path_consistent(found[1]):
            found = None
        reasons, _ports = diagnose(solver, src, dst, ideal, set())
        if not found:
            reasons = ["当前回溯状态下不存在可与已规划路由共存的通路"] + reasons
        unsatisfied.append({
            "index": idx, "source": src, "target": dst, "group": groups[idx],
            "status": "unroutable", "reasons": reasons,
        })
    unsatisfied.sort(key=lambda x: x["index"])

    components, _f, _p = _components(
        solver, devices, pairs, groups, node, commits, forbidden)

    # 贪心对照拓扑（仅展示）：切到贪心节点生成摘要后切回终止节点
    bind(solver, greedy_node)
    g_pairs = pairs_summary(pairs, solver)
    g_components, _gf, _gp = _components(
        solver, devices, pairs, groups, greedy_node, greedy_node.commits, forbidden)
    bind(solver, node)
    term_pairs = pairs_summary(pairs, solver)

    groups_out = []
    for name in sorted({g for g in groups if g}):
        groups_out.append({"name": name, "routeIndexes": routes_of(name)})

    return {
        "ok": False,
        "isolated": False,
        "inconclusive": inconclusive,
        "solverNote": note,
        "searchNodes": stats["nodes"],
        "groups": groups_out,
        "forbiddenPairs": [
            {"a": a, "b": b,
             "satisfied": (a, b) not in live_bad and (a, b) not in perm_bad_all
                          and (a, b) not in structural_bad}
            for a, b in forbidden],
        "conflicts": conflicts_out,
        "unsatisfiedRoutes": unsatisfied,
        "components": components,
        "pairs": term_pairs,
        "cables": [dict(c) for c in node.cables],
        "terminal": {"complete": best["complete"],
                     "committedRoutes": sorted(node.order),
                     "cableCount": node.cable_count},
        # 输入顺序贪心的物理拓扑：仅供页面冲突高亮展示，不是可施工方案
        "baseline": {
            "pairs": g_pairs,
            "cables": [dict(c) for c in greedy_node.cables],
            "components": g_components,
            "violatedPairs": greedy_meta["violatedPairs"],
            "permanentViolatedPairs": greedy_meta["permanentViolatedPairs"],
            "unroutable": greedy_meta["unroutable"],
            "greedyCableCount": greedy_meta["cableCount"],
        },
    }
