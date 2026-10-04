"""串音隔离规划（crosstalk isolation planning）。

在原有跳线盘规划模型（设备内部 in→out、全/半常态、直通、前面板跳线）
之上增加**隔离组**约束：用户为每条路由填写隔离组，并声明若干
**禁配组对**——两个组中的路由，其信号在最终物理状态下不得落入同一个
**物理连通分量**（无向图，边为：设备内部连接、生效常态连接、前面板
跳线）。半常态上插孔插线时常态保持、信号并送，因此并送两侧仍属同一
分量；全常态任一侧插线即断开。

规划算法：

- 按用户给定顺序逐条取**贪心最短路**（与 solver.plan_all 完全一致的
  0-1 Dijkstra）先试一次：这就是“按输入顺序的贪心方案”；
- 贪心方案出现禁配组共享分量、或某条路由在当前占用下无路可走时
  **回溯**：先为当前路由枚举非最短的备选简单路径（断开共享常态、
  改走跳线）；备选枚举被上限截断、固定顺序仍无法证明无解时，第二阶段
  再**重排已规划路由**（确定性种子排列）后重新搜索；
- 每提交一条路径就用**只含永久边**（设备内部、已提交跳线、被保护
  常态、上插孔已并送的半常态、两孔都已被占用的生效常态）的可回滚
  并查集做提前剪枝：两个禁配组一旦沿永久边连通，后续路由只能加跳线、
  拔不掉这些边，不必再搜；未被使用的空闲全常态随时可能被插断，不是
  永久边，只在叶端完整物理图上终判（宁可漏剪枝，不可误剪可行解）；
- 全部路由提交后在完整物理图（还包含当前仍生效、未被任何路由使用的
  常态）上终判；反馈环检测照旧保留；
- 优化目标字典序：**跳线总数最少 → 提交顺序尽量贴近输入顺序**，
  搜索节点超限时返回 inconclusive，不伪称无解；
- 无解 / 搜索未证毕时**不提交任何方案**，返回冲突组、每组之间的
  **最短共享证据**（多源 BFS）与未满足路由清单。
"""

from collections import defaultdict, deque

from solver import (HALF, MODE_LABELS, THROUGH, ConfigError, Solver,
                    compute_failure_points, detect_loops, pairs_summary,
                    parse_config, plug_note, _pid)

SEARCH_NODE_CAP = 300_000              # 回溯搜索节点上限（与 rewire/layout 一致）
ENUM_DFS_STEPS = 200_000               # 单条路由备选路径枚举的 DFS 步数上限
ENUM_PATH_CAP = 256                    # 单条路由保留的备选路径条数上限
MAX_CABLES_IN_PATH = 5                 # 单条路径的跳线数上限（枚举边界）


class IsolationError(ConfigError):
    """串音隔离规划相关错误。

    code:
      ISOLATION_INVALID       隔离声明非法（400）
      ISOLATION_UNSAT         回溯证毕仍无解（409，payload 携带完整诊断）
      ISOLATION_INCONCLUSIVE  搜索超限，未证毕（409，payload 携带贪心诊断）
    """

    def __init__(self, message, code="ISOLATION_INVALID", details=None, payload=None):
        super().__init__(message, code=code, details=details or {})
        self.payload = payload


# ---- 隔离声明解析 ---------------------------------------------------------


def parse_isolation(config, routes):
    """从原始配置解析隔离组与禁配组对。

    返回 (group_of, group_order, forbidden)：
      group_of    {路由序号: 组 id}；未填组的路由不在字典中（中性路由）
      group_order 组 id 按首次出现顺序排列
      forbidden   [(a, b), ...]，已去重、按声明顺序
    """
    group_of = {}
    group_order = []
    raw_routes = config.get("routes") if isinstance(config, dict) else None
    for i, r in enumerate(raw_routes or []):
        if not isinstance(r, dict):
            continue
        raw = r.get("isolationGroup", r.get("group"))
        if raw is None:
            continue
        if not isinstance(raw, str):
            raise IsolationError(f"路由 {i + 1}：隔离组必须是字符串名称")
        g = raw.strip()
        if not g:
            continue
        if "." in g:
            raise IsolationError(f"隔离组名不能包含点号: {g}")
        group_of[i] = g
        if g not in group_order:
            group_order.append(g)

    iso = config.get("isolation") if isinstance(config, dict) else None
    if iso is None:
        iso = {}
    if not isinstance(iso, dict):
        raise IsolationError("isolation 必须是 JSON 对象，如 {\"forbiddenPairs\": [[\"A\", \"B\"]]}")
    raw_pairs = iso.get("forbiddenPairs", [])
    if raw_pairs is None:
        raw_pairs = []
    if not isinstance(raw_pairs, list):
        raise IsolationError("isolation.forbiddenPairs 必须是数组，如 [[\"A\", \"B\"]]")

    def as_group(v, ctx):
        if not isinstance(v, str):
            raise IsolationError(f"{ctx}：隔离组必须是字符串名称，得到 {v!r}")
        g = v.strip()
        if not g:
            raise IsolationError(f"{ctx}：隔离组名不能为空")
        if g not in group_order:
            raise IsolationError(f"{ctx}：隔离组 “{g}” 尚未分配给任何路由，"
                                 f"请先在路由表中填写该组")
        return g

    forbidden, seen = [], set()
    for i, fp in enumerate(raw_pairs):
        ctx = f"第 {i + 1} 个禁配组对"
        a = b = None
        if isinstance(fp, (list, tuple)):
            if len(fp) != 2:
                raise IsolationError(f"{ctx}：必须恰好包含两个组名")
            a, b = fp
        elif isinstance(fp, dict):
            a = fp.get("a", fp.get("groupA"))
            b = fp.get("b", fp.get("groupB"))
            if (a is None or b is None) and isinstance(fp.get("groups"), (list, tuple)):
                if len(fp["groups"]) != 2:
                    raise IsolationError(f"{ctx}：groups 必须恰好包含两个组名")
                a, b = fp["groups"]
        else:
            raise IsolationError(f"{ctx}：必须是 [组A, 组B] 或 {{\"a\": ..., \"b\": ...}}")
        ga, gb = as_group(a, ctx), as_group(b, ctx)
        if ga == gb:
            raise IsolationError(f"{ctx}：组不能与自身禁配（{ga}）")
        key = tuple(sorted((ga, gb)))
        if key not in seen:
            seen.add(key)
            forbidden.append(key)
    return group_of, group_order, forbidden


# ---- 备选路径枚举 ---------------------------------------------------------


def path_key(items):
    return tuple(p for kind, p in items if kind == "port")


def enumerate_alternatives(solver, src, dst, skip_keys,
                           path_cap=ENUM_PATH_CAP, step_cap=ENUM_DFS_STEPS):
    """枚举 src→dst 的可行简单路径（遵守当前插孔占用与常态保护）。

    与 Solver.find_path 使用同一组邻居规则，但不只取最短路：按 DFS
    收集不同端口序列，供回溯在“贪心最短路导致串音”时换线。
    返回 (paths, truncated)：paths 为 [(cable_count, items), ...]，
    按（跳线数, 端口序列）排序，已剔除 skip_keys。
    """
    found = {}
    state = {"steps": 0, "truncated": False}

    def dfs(u, tail, plugs, cable_cnt, visited):
        state["steps"] += 1
        if state["steps"] > step_cap or len(found) >= path_cap:
            state["truncated"] = True
            return
        if u == dst:
            key = tuple(p for kind, p in tail if kind == "port")
            if key not in skip_keys and key not in found:
                found[key] = (cable_cnt, list(tail))
            return

        name, direction = solver.port_dir[u]
        if direction == "in":
            for o in solver.devices[name]["outputs"]:
                v = f"{name}.{o}"
                if v in visited:
                    continue
                visited.add(v)
                tail.append(("edge", ("device", name)))
                tail.append(("port", v))
                dfs(v, tail, plugs, cable_cnt, visited)
                tail.pop()
                tail.pop()
                visited.discard(v)
            return

        # 0 跳线：生效常态（计入本路径自身插头的影响）
        if u in solver.normal_from:
            ndst, pair = solver.normal_from[u]
            if (pair["id"] not in solver.broken_normals
                    and solver.normal_active(pair, extra_plugs=plugs)
                    and ndst not in visited):
                visited.add(ndst)
                tail.append(("edge", ("normal", pair)))
                tail.append(("port", ndst))
                dfs(ndst, tail, plugs, cable_cnt, visited)
                tail.pop()
                tail.pop()
                visited.discard(ndst)

        # 1 跳线：当前输出插孔 → 任意空闲且不破坏受保护常态的输入插孔
        if cable_cnt < MAX_CABLES_IN_PATH:
            ju = solver.port_jack.get(u)
            if ju and ju not in solver.plugs and ju not in plugs \
                    and not solver.cable_forbidden(ju):
                for v, jv in solver.input_jacks:
                    if jv == ju or jv in solver.plugs or jv in plugs \
                            or solver.cable_forbidden(jv) or v in visited:
                        continue
                    visited.add(v)
                    new_plugs = plugs | {ju, jv}
                    tail.append(("edge", ("cable", ju, jv)))
                    tail.append(("port", v))
                    dfs(v, tail, new_plugs, cable_cnt + 1, visited)
                    tail.pop()
                    tail.pop()
                    visited.discard(v)
                    if state["truncated"]:
                        return

    dfs(src, [("port", src)], frozenset(), 0, {src})
    paths = sorted(found.values(), key=lambda ci: (ci[0], path_key(ci[1])))
    if len(found) >= path_cap or state["steps"] > step_cap:
        state["truncated"] = True
    return paths, state["truncated"]


# ---- 物理图 / 连通分量 / 证据 --------------------------------------------


def build_physical_adj(devices, pairs, cables, solver):
    """完整无向物理图：设备内部（同设备输入↔输出完全相连）+ 生效常态 + 跳线。"""
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
    for p in pairs:
        if p["mode"] != THROUGH and p["top"] and p["bottom"] \
                and solver.normal_active(p):
            a, b = _pid(p["top"]), _pid(p["bottom"])
            link(a, b, {"type": "normal", "pairId": p["id"],
                        "mode": p["mode"],
                        "label": f"{MODE_LABELS[p['mode']]} · 第{p['id']}对"})
    for c in cables:
        link(c["fromPort"], c["toPort"],
             {"type": "cable", "cableId": c["id"], "label": f"跳线 #{c['id']}"})
    for port in solver.port_dir:
        adj.setdefault(port, [])
    return adj


def bfs_shortest(adj, starts, goals):
    """多源 BFS：starts 任一端口到 goals 任一端口的最短无向路径。返回端口列表。"""
    starts, goals = set(starts), set(goals)
    common = starts & goals
    if common:
        return [min(common)]
    prev = {s: None for s in starts}
    q = deque(sorted(starts))
    hit = None
    while q and hit is None:
        u = q.popleft()
        for v, _edge in adj.get(u, ()):
            if v in prev:
                continue
            prev[v] = u
            if v in goals:
                hit = v
                break
            q.append(v)
    if hit is None:
        return None
    nodes, x = [], hit
    while x is not None:
        nodes.append(x)
        x = prev[x]
    nodes.reverse()
    return nodes


def edge_between(adj, a, b):
    for v, edge in adj.get(a, ()):
        if v == b:
            return edge
    return None


def evidence_chain(nodes, adj, solver):
    """端口序列 → 前端可渲染的 port/edge 交替列表。"""
    items = []
    for i, node in enumerate(nodes):
        items.append({"kind": "port", "port": node,
                      "jack": solver.port_jack.get(node)})
        if i + 1 < len(nodes):
            edge = edge_between(adj, node, nodes[i + 1]) or \
                {"type": "link", "label": "连通"}
            items.append({"kind": "edge", **edge})
    return items


def label_components(adj):
    """按 adj 插入顺序 BFS 标注连通分量，返回 {port: comp_id}。"""
    comp_of, cid = {}, 0
    for start in adj:
        if start in comp_of:
            continue
        comp_of[start] = cid
        q = deque([start])
        while q:
            u = q.popleft()
            for v, _e in adj.get(u, ()):
                if v not in comp_of:
                    comp_of[v] = cid
                    q.append(v)
        cid += 1
    return comp_of


def components_payload(adj, touched, solver, comp_of=None):
    """承载路由的物理分量：每个端口带触及它的隔离组与路由序号。"""
    if comp_of is None:
        comp_of = label_components(adj)
    groups_of_port, routes_of_port = defaultdict(set), defaultdict(set)
    for port, info in touched.items():
        for g in info["groups"]:
            groups_of_port[port].add(g)
        for r in info["routes"]:
            routes_of_port[port].add(r)

    buckets = defaultdict(list)
    for port in adj:
        if port in touched:
            buckets[comp_of[port]].append(port)
    out = []
    for cid in sorted(buckets):
        ports = [{
            "port": p,
            "jack": solver.port_jack.get(p),
            "groups": sorted(groups_of_port.get(p, ())),
            "routes": sorted(routes_of_port.get(p, ())),
        } for p in buckets[cid]]
        groups = sorted({g for p in ports for g in p["groups"]})
        out.append({"id": len(out), "componentId": cid,
                    "groups": groups, "ports": ports})
    return out


# ---- 可回滚并查集（只含永久边的提前剪枝） ---------------------------------


class RollbackDSU:
    def __init__(self, nodes):
        self.parent = {n: n for n in nodes}
        self.size = {n: 1 for n in nodes}
        self.undo = []

    def find(self, x):
        while self.parent[x] != x:
            x = self.parent[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra == rb:
            return False
        if self.size[ra] < self.size[rb]:
            ra, rb = rb, ra
        self.undo.append(rb)
        self.parent[rb] = ra
        self.size[ra] += self.size[rb]
        return True

    def mark(self):
        return len(self.undo)

    def rollback(self, mark):
        while len(self.undo) > mark:
            rb = self.undo.pop()
            ra = self.parent[rb]
            self.parent[rb] = rb
            self.size[ra] -= self.size[rb]


# ---- 隔离冲突终判 ---------------------------------------------------------


def isolation_violations(adj, group_ports, forbidden, solver):
    """在完整物理图上找出仍共享连通分量的禁配组对（附最短共享证据）。"""
    comp_of = label_components(adj)
    violations = []
    for a, b in forbidden:
        pa, pb = group_ports.get(a, set()), group_ports.get(b, set())
        if not pa or not pb:
            continue
        if not any(comp_of.get(x) == comp_of.get(y)
                   for x in pa for y in pb if x in comp_of and y in comp_of):
            continue
        nodes = bfs_shortest(adj, pa, pb)
        if nodes is None:
            continue
        violations.append({
            "groups": [a, b],
            "component": comp_of[nodes[0]],
            "evidence": evidence_chain(nodes, adj, solver),
        })
    return violations


# ---- 主搜索 ---------------------------------------------------------------


def snapshot_solver(s):
    return (dict(s.plugs),
            [dict(c) for c in s.cables],
            s.next_cid,
            set(s.protected_normals))


def restore_solver(s, snap):
    plugs, cables, next_cid, protected = snap
    s.plugs = plugs
    s.cables = cables
    s.next_cid = next_cid
    s.protected_normals = protected


def route_ports(items):
    return [p for kind, p in items if kind == "port"]


def plan_isolation(config):
    """带串音隔离约束的全量规划。成功返回结果字典；失败抛 IsolationError。

    搜索分两个阶段：
      阶段一（固定输入顺序回溯）——按输入顺序逐条规划，每条先试贪心最短路，
      失败再枚举备选路径；可行的最终“跳线+占用”分配与提交顺序无关
      （常态保护规则对任何可行最终分配都存在可行提交序），因此固定顺序
      足以找到跳线最少的最优解。
      阶段二（回溯重排）——仅当阶段一的备选路径枚举被上限截断、无法证明
      无解时启动：放宽为“可重排已规划路由”的排列搜索，重新给一次
      节点预算，覆盖枚举截断导致的漏解。
    """
    devices, pairs, routes, port_jack = parse_config(config)
    group_of, group_order, forbidden = parse_isolation(config, routes)
    n = len(routes)

    # 每条路由在“空板”下独立可达的最少跳线数：任何方案跳线总数的理论下界
    # （必须每条路由各自一块空板求解，顺序提交会因占用而高估，不能作下界）
    lower_bound = 0
    for r in routes:
        empty = Solver(devices, pairs, routes, port_jack)
        found = empty.find_path(_pid(r["source"]), _pid(r["target"]))
        if found and empty.path_consistent(found[1]):
            lower_bound += found[0]

    stats = {"nodes": 0, "backtracks": 0,
             "alternativePaths": 0, "truncated": False}

    def run_search(order, node_budget, allow_reorder):
        """在给定路由顺序上做“贪心 + 备选路径”回溯。

        返回 best：{cables, order, entries, reordered} 或 None（未找到 / 超限）。
        """
        s = Solver(devices, pairs, routes, port_jack)
        dsu = RollbackDSU(list(s.port_dir))
        local = {"nodes": 0, "truncated": False}
        best = {"cables": None, "order": None, "entries": None}
        # 已确认永久存在的生效常态编号（回滚时整体恢复）
        permanent_normals = set()

        def union_permanent_normals():
            """把提交后新确认“永久生效”的常态并入并查集。

            永久生效的三类：
              1. 被已提交路由使用 → 进入 protected_normals，后续插线
                 不可能再断开它；
              2. 半常态且上插孔已插跳线（信号并送）：只要下插孔不插就
                 永久保持；而下插孔一旦插线就会断开它，届时若已有路由
                 依赖它本就会被 cable_forbidden 禁止——因此当前生效即永久。
              3. 两个插孔都已被已提交跳线占用：没有空闲插孔可再插入插头，
                 全常态也永远不会被插断。
            全常态在两侧都空时虽然生效，但任一侧可被任意未来跳线插断，
            不是永久边，不能并入（宁可叶端终判，不可误剪）。
            """
            added = []
            for p in pairs:
                pid = p["id"]
                if pid in permanent_normals:
                    continue
                if p["mode"] == THROUGH or not (p["top"] and p["bottom"]):
                    continue
                tj, bj = f"{pid}T", f"{pid}B"
                protected = pid in s.protected_normals
                half_tapped = (p["mode"] == HALF
                               and tj in s.plugs
                               and s.normal_active(p))
                both_occupied = tj in s.plugs and bj in s.plugs
                if (protected or half_tapped or both_occupied) and s.normal_active(p):
                    dsu.union(_pid(p["top"]), _pid(p["bottom"]))
                    permanent_normals.add(pid)
                    added.append(pid)
            return added

        def dfs(pos, entries, group_ports):
            local["nodes"] += 1
            stats["nodes"] += 1
            if local["nodes"] > node_budget:
                local["truncated"] = True
                stats["truncated"] = True
                return
            if pos == n:
                # 终判在完整物理图上再做一次（兜底永久边剪枝之外的细节）
                adj = build_physical_adj(devices, pairs, s.cables, s)
                if isolation_violations(adj, group_ports, forbidden, s):
                    return
                if best["cables"] is None or len(s.cables) < best["cables"]:
                    best["cables"] = len(s.cables)
                    best["order"] = tuple(order)
                    best["entries"] = dict(entries)
                return
            if best["cables"] is not None and len(s.cables) > best["cables"]:
                return
            if best["cables"] is not None and best["cables"] <= lower_bound:
                return

            nxt = order[pos]
            r = routes[nxt]
            src, dst = _pid(r["source"]), _pid(r["target"])

            # 候选 1：与原规划完全一致的贪心最短路
            candidates = []
            found = s.find_path(src, dst)
            if found and s.path_consistent(found[1]):
                candidates.append((found[0], found[1]))
            # 候选 2..：非最短备选路径（贪心导致串音 / 占用死锁时换线）。
            # 中性路由（未填隔离组）不可能参与任何禁配违例，贪心最短路
            # 对它永远安全；枚举其海量换线只会让搜索组合爆炸，故跳过。
            if nxt in group_of:
                skip = {path_key(c[1]) for c in candidates}
                alts, trunc = enumerate_alternatives(s, src, dst, skip)
                if trunc:
                    local["truncated"] = True
                    stats["truncated"] = True
                if alts:
                    stats["alternativePaths"] += 1
                candidates.extend(alts)

            for cc, items in candidates:
                if best["cables"] is not None and len(s.cables) + cc > best["cables"]:
                    continue
                snap = snapshot_solver(s)
                mark = dsu.mark()
                perm_before = set(permanent_normals)
                s.commit(items)
                ports = route_ports(items)
                # 路径上的每一条边（设备/跳线/受保护常态）提交后永久存在：
                # 跳线不可拆、常态一经使用即受保护、设备边恒定。
                for u, v in zip(ports, ports[1:]):
                    dsu.union(u, v)
                union_permanent_normals()
                ngp = group_ports
                g = group_of.get(nxt)
                if g is not None:
                    ngp = {k: set(v) for k, v in group_ports.items()}
                    ngp.setdefault(g, set()).update(ports)
                clash = False
                for a, b in forbidden:
                    pa, pb = ngp.get(a), ngp.get(b)
                    if pa and pb:
                        # 同一物理端口被两个禁配组的路由共同触及（如同源
                        # 端口、同一目标端口）——无需任何边就已经共享
                        if pa & pb:
                            clash = True
                            break
                        if any(dsu.find(x) == dsu.find(y)
                               for x in pa for y in pb):
                            clash = True
                            break
                if not clash:
                    entries[nxt] = {"items": items, "snap": snapshot_solver(s)}
                    dfs(pos + 1, entries, ngp)
                stats["backtracks"] += 1
                dsu.rollback(mark)
                restore_solver(s, snap)
                permanent_normals.intersection_update(perm_before)
                entries.pop(nxt, None)
                if local["truncated"]:
                    return

        dfs(0, {}, defaultdict(set))
        if best["cables"] is not None:
            best["reordered"] = allow_reorder and list(order) != list(range(n))
        return None if best["cables"] is None else best

    # 阶段一：严格按输入顺序的贪心 + 备选回溯
    best = run_search(tuple(range(n)), SEARCH_NODE_CAP, False)

    # 阶段二：仅当阶段一被枚举/节点上限截断时，回溯重排已规划路由重试
    if best is None and stats["truncated"]:
        # 以贪心基线里“最可能制造冲突”的组为线索重排：让互禁组尽量间隔，
        # 但确定性穷举排列太贵——优先尝试少数几个种子排列（贪心成功序、
        # 组间交替序、逆序），每个给一份独立节点预算。
        seed_orders = reorder_seed_orders(routes, group_of)
        for od in seed_orders:
            if tuple(od) == tuple(range(n)):
                continue
            best = run_search(tuple(od), SEARCH_NODE_CAP // 4, True)
            if best is not None and best["cables"] <= lower_bound:
                break

    if best is not None:
        return build_success(devices, pairs, routes, port_jack,
                             group_of, group_order, forbidden, best, stats)

    # 无可行方案：以输入顺序贪心方案为诊断基线，收集冲突组 / 最短证据 /
    # 未满足路由；绝不提交半成品。
    return build_failure(config, devices, pairs, routes, port_jack,
                         group_of, group_order, forbidden, stats)


def reorder_seed_orders(routes, group_of):
    """回溯重排阶段尝试的确定性种子排列。

    包含：原序、逆序、按隔离组交替（A,B,A,B…，把互禁组在时间上拉开）、
    有组路由优先、有组路由靠后。全部为稳定可复现排列。
    """
    n = len(routes)
    base = list(range(n))
    orders = [base, list(reversed(base))]

    # 组交替：按组首次出现顺序轮转取号
    groups = []
    for i in base:
        g = group_of.get(i)
        if g is not None and g not in groups:
            groups.append(g)
    if len(groups) >= 2:
        buckets = {g: [] for g in groups}
        neutral = []
        for i in base:
            g = group_of.get(i)
            (buckets[g] if g is not None else neutral).append(i)
        alt, ptr = [], 0
        alive = set(groups)
        while alive:
            g = groups[ptr % len(groups)]
            if g in alive:
                if buckets[g]:
                    alt.append(buckets[g].pop(0))
                if not buckets[g]:
                    alive.discard(g)
            ptr += 1
        orders.append(alt + neutral)

    with_group = [i for i in base if i in group_of]
    no_group = [i for i in base if i not in group_of]
    orders.append(with_group + no_group)
    orders.append(no_group + with_group)

    # 去重保序
    seen, uniq = set(), []
    for od in orders:
        t = tuple(od)
        if t not in seen and sorted(t) == base:
            seen.add(t)
            uniq.append(list(t))
    return uniq


# ---- 成功结果 -------------------------------------------------------------


def build_success(devices, pairs, routes, port_jack,
                  group_of, group_order, forbidden, best, stats):
    order = list(best["order"])
    entries = best["entries"]

    # 直接恢复搜索末态的求解器快照：最优解的提交顺序可能与输入顺序不同，
    # 此时按输入顺序重放并不可行（这正是需要重排的原因）。
    last = entries[order[-1]]["snap"]
    s = Solver(devices, pairs, routes, port_jack)
    s.plugs = dict(last[0])
    s.cables = [dict(c) for c in last[1]]
    s.next_cid = last[2]
    s.protected_normals = set(last[3])

    routes_out, planned = [], []
    commit_order_of = {idx: k for k, idx in enumerate(order)}
    for k, idx in enumerate(order):
        r = routes[idx]
        src, dst = _pid(r["source"]), _pid(r["target"])
        items = entries[idx]["items"]
        # 用末态快照里的跳线号构造与 build_chain 兼容的 cable_ids 映射
        cable_ids = {}
        for kind, payload in items:
            if kind == "edge" and payload[0] == "cable":
                fj, tj = payload[1], payload[2]
                cable_ids[(fj, tj)] = s.plugs.get(fj)
        normals, cable_pairs = Solver.extract_usage(items)
        used_cables = sorted({cid for cid in cable_ids.values() if cid is not None})
        routes_out.append({
            "index": idx, "source": src, "target": dst, "status": "ok",
            "isolationGroup": group_of.get(idx),
            "commitOrder": k,
            "cables": used_cables,
            "chain": s.build_chain(items, cable_ids),
        })
        planned.append({"index": idx, "source": src, "target": dst,
                        "ok": True, "items": items, "cableIds": used_cables,
                        "normals": normals, "cablePairs": cable_pairs})
    routes_out.sort(key=lambda r: r["index"])

    loops = detect_loops(devices, pairs, s.cables, s.normal_active)
    notes = []
    for c in s.cables:
        for jack in (c["fromJack"], c["toJack"]):
            note = plug_note(s.pair_of_jack[jack], jack, s.jack_port)
            if note and note not in notes:
                notes.append(note)
    steps = [
        f"第 {c['id']} 步：取跳线 #{c['id']}，一端插入前面板 {c['fromJack']}"
        f"（{c['fromPort']}），另一端插入 {c['toJack']}（{c['toPort']}）"
        for c in s.cables
    ]
    pairs_out = pairs_summary(pairs, s)
    failure_points = compute_failure_points(planned, s)

    # 最终物理分量（含每组触及端口，供前端拓扑着色）
    touched = defaultdict(lambda: {"groups": set(), "routes": set()})
    for idx in order:
        items = entries[idx]["items"]
        g = group_of.get(idx)
        for kind, p in items:
            if kind != "port":
                continue
            if g is not None:
                touched[p]["groups"].add(g)
            touched[p]["routes"].add(idx)
    adj = build_physical_adj(devices, pairs, s.cables, s)
    components = components_payload(adj, touched, s)

    reordered = list(best["order"]) != list(range(len(routes)))
    groups_out = [{
        "id": g,
        "routes": sorted(i for i, gg in group_of.items() if gg == g),
    } for g in group_order]

    return {
        "ok": True,
        "isolationOk": True,
        "cableCount": len(s.cables),
        "cables": s.cables,
        "steps": steps,
        "notes": notes,
        "routes": routes_out,
        "conflicts": [],
        "feedbackLoops": loops,
        "pairs": pairs_out,
        "failurePoints": failure_points,
        "groups": groups_out,
        "forbiddenPairs": [list(p) for p in forbidden],
        "components": components,
        "violations": [],
        "unsatisfiedRoutes": [],
        "planningOrder": order,
        "search": {
            "nodes": stats["nodes"],
            "backtracks": stats["backtracks"],
            "alternativePaths": stats["alternativePaths"],
            "reordered": reordered,
            "truncated": False,
        },
    }


# ---- 失败诊断 -------------------------------------------------------------


def build_failure(config, devices, pairs, routes, port_jack,
                  group_of, group_order, forbidden, stats):
    """贪心基线（输入顺序）上的完整隔离诊断；不提交任何方案。"""
    from solver import plan_all

    baseline_result, baseline_plan = plan_all(config)
    s = baseline_plan["solver"]

    # 每条贪心成功路由触及的端口
    touched_ports = defaultdict(lambda: {"groups": set(), "routes": set()})
    chain_of = {}
    for info in baseline_plan["planned"]:
        if not info["ok"]:
            continue
        idx = info["index"]
        g = group_of.get(idx)
        ports = [p for kind, p in info["items"] if kind == "port"]
        chain_of[idx] = ports
        for p in ports:
            if g is not None:
                touched_ports[p]["groups"].add(g)
            touched_ports[p]["routes"].add(idx)

    adj = build_physical_adj(devices, pairs, s.cables, s)
    group_ports = defaultdict(set)
    for port, info in touched_ports.items():
        for g in info["groups"]:
            group_ports[g].add(port)
    violations = isolation_violations(adj, group_ports, forbidden, s)
    comp_of = label_components(adj)
    components = components_payload(adj, touched_ports, s, comp_of=comp_of)

    # 组 → 路由；路由 → 其参与的违例组对
    routes_of_group = defaultdict(list)
    for idx, g in group_of.items():
        routes_of_group[g].append(idx)
    violation_of_route = defaultdict(list)
    for v in violations:
        a, b = v["groups"]
        for idx in routes_of_group.get(a, []):
            violation_of_route[idx].append(v)
        for idx in routes_of_group.get(b, []):
            violation_of_route[idx].append(v)

    unsatisfied = []
    conflict_by_route = {c["route"]: c for c in baseline_plan["conflicts"]}
    involved = set(violation_of_route) | set(conflict_by_route)
    for idx in sorted(involved):
        r = routes[idx]
        src, dst = _pid(r["source"]), _pid(r["target"])
        g = group_of.get(idx)
        if idx in conflict_by_route:
            c = conflict_by_route[idx]
            extra = []
            for v in violation_of_route.get(idx, ()):
                a, b = v["groups"]
                other = b if a == g else a
                extra.append(f"其隔离组还与 “{other}” 共享物理连通分量"
                             f"（最短共享证据 {evidence_len(v['evidence'])} 跳）")
            unsatisfied.append({
                "index": idx, "source": src, "target": dst,
                "isolationGroup": g, "status": "unroutable",
                "reasons": c["reasons"] + extra, "ports": c["ports"],
            })
        else:
            pair_labels = []
            for v in violation_of_route[idx]:
                a, b = v["groups"]
                other = b if a == g else a
                pair_labels.append(f"与隔离组 “{other}” 落入同一物理连通分量"
                                   f"（最短共享证据 {evidence_len(v['evidence'])} 跳）")
            unsatisfied.append({
                "index": idx, "source": src, "target": dst,
                "isolationGroup": g, "status": "isolated",
                "reasons": pair_labels,
                "violations": [v["groups"] for v in violation_of_route[idx]],
            })

    conflict_groups = sorted({g for v in violations for g in v["groups"]})
    groups_out = [{"id": g, "routes": sorted(routes_of_group.get(g, []))}
                  for g in group_order]

    loops = baseline_plan["loops"]
    baseline_routes = []
    for rr in baseline_result["routes"]:
        rr = dict(rr)
        rr["isolationGroup"] = group_of.get(rr["index"])
        rr["commitOrder"] = rr["index"]
        baseline_routes.append(rr)
    payload = {
        "ok": False,
        "isolationOk": False,
        "cableCount": len(s.cables),
        "cables": [dict(c) for c in s.cables],
        "steps": baseline_result["steps"],
        "notes": baseline_result["notes"],
        "routes": baseline_routes,
        "conflicts": baseline_result["conflicts"],
        "feedbackLoops": loops,
        "pairs": baseline_plan["pairs_out"],
        "failurePoints": baseline_plan["failure_points"],
        "groups": groups_out,
        "forbiddenPairs": [list(p) for p in forbidden],
        "components": components,
        "violations": violations,
        "conflictGroups": conflict_groups,
        "unsatisfiedRoutes": unsatisfied,
        "planningOrder": None,
        "search": {
            "nodes": stats["nodes"],
            "backtracks": stats["backtracks"],
            "alternativePaths": stats["alternativePaths"],
            "reordered": False,
            "truncated": bool(stats["truncated"]),
        },
    }

    if stats["truncated"]:
        msg = (f"串音隔离规划搜索超过 {SEARCH_NODE_CAP} 个节点（或备选路径枚举"
               f"达到上限），未能证明无解，也未提交方案；以下为输入顺序贪心"
               f"方案的隔离诊断")
        err = IsolationError(msg, code="ISOLATION_INCONCLUSIVE", payload=payload)
    else:
        if violations and conflict_by_route:
            msg = (f"串音隔离无可行方案：{len(violations)} 对禁配组共享物理连通"
                   f"分量，且 {len(conflict_by_route)} 条路由在回溯重排后仍无可用"
                   f"通路；未提交任何方案")
        elif violations:
            msg = (f"串音隔离无可行方案：{len(violations)} 对禁配组在全部可行"
                   f"路由选择下仍共享物理连通分量；未提交任何方案")
        else:
            msg = (f"串音隔离无可行方案：{len(conflict_by_route)} 条路由在回溯"
                   f"重排后仍无可用通路；未提交任何方案")
        err = IsolationError(msg, code="ISOLATION_UNSAT", payload=payload)
    err.payload = payload
    raise err


def evidence_len(evidence):
    return sum(1 for x in evidence if x["kind"] == "edge")
