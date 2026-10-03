"""信号命名一致性审计。

在**不改动任何路由结论**的前提下，复用 ``solver.plan_all`` 得到规划完成后的
物理连通关系（设备内部通路 + 生效常态连接 + 已安装跳线），把用户在设备端口、
插孔对、已规划跳线与路由上填写的**规范信号名**与**别名**沿这些连接传播，
按半常态分流语义归并端点（半常态上插孔插线时常态保持、信号并送，因此
上/下端口仍属同一连通分量，并额外构成一个需要命名的“分流点”），检测四类问题：

  mutex       同一物理连通分量上出现互斥的规范信号名；
  alias-ring  别名指向成环（含同一别名被指向多个不同规范名的分叉声明）；
  unlabeled   承载路由信号的跳线 / 半常态分流点没有任何标签（无标签断点）；
  crosstalk   来自**独立信号源**的已规划路由在同一连通分量汇合（跨路由串线）。

每个问题都给出：

  - 两个命名端点（串线为两组独立源端口）之间的**最短证据路径**
    （多源 BFS，沿无向物理图）；
  - 可采纳的规范名（分量内声明次数最多、并列时取字典序最小者）；
  - 一组**修正预览**（把落败名降级为别名的 rename / 补标签的 add-label），
    仅供前端预览与回填，审计本身不修改路由，也不回写任何配置。

跨路由串线**与名签完全解耦**：判定只依据路由规划与物理连通。每条已规划
路由的信号源即其源端口；同一源端口的路由天然同源。源端口在有向信号流图
（设备内部 in→out、生效常态 上→下、跳线 from→to）上同属一个强连通分量
（反馈环 SCC）的合并为一路。在此基础上，审计还会从每个路由源沿信号方向
**反向追溯**设备内部、生效常态与跳线的共同上游：多个输出端口（如同一台
多输出设备的两个输出、或被同一信号推动的常态/跳线分支）若都从同一个
上游终源 SCC 衍生，则它们只是该独立源的同源分支，归为同一来源，不计串线；
只有反向终源互不相同（真正的多源混合、半常态并送汇合）时才报串线。
即便路由列表里**只有混合输出**（如仅 MIX.out→MON.in 一条、两个真实源分别
经生效常态进入 MIX.in1/in2），审计也会从该混合源的终源集合反向恢复出真实
独立源，把混合输出路由同时归属各源并照常报一条串线，不会因混合点自身被归
为 mix 而漏报。
串线**不提供可消警的改名操作**，只有调整路由规划或物理连通才能消除。
"""

from collections import defaultdict, deque

from solver import MODE_LABELS, THROUGH, ConfigError, plan_all

MUTEX = "mutex"
ALIAS_RING = "alias-ring"
UNLABELED = "unlabeled"
CROSSTALK = "crosstalk"

TYPE_TITLES = {
    MUTEX: "互斥名称",
    ALIAS_RING: "别名环",
    UNLABELED: "无标签断点",
    CROSSTALK: "跨路由串线",
}
ISSUE_LIMIT = 24                       # 单类问题最多返回条数，避免极端配置刷屏


class SignalAuditError(ConfigError):
    """命名/标签配置非法。"""

    def __init__(self, message, details=None):
        super().__init__(message, code="LABELS_INVALID", details=details or {})


# ---- 标签解析 -------------------------------------------------------------


def _text(v):
    if v is None:
        return None
    if not isinstance(v, str):
        raise SignalAuditError("规范名与别名必须是字符串")
    t = v.strip()
    return t or None


def _origin_label(kind, key, port=None):
    if kind == "port":
        return f"端口 {port}"
    if kind == "pair":
        return f"插孔对第 {key} 对"
    if kind == "cable":
        return f"跳线 #{key}"
    return f"路由 {key + 1}"


def parse_labels(raw, plan):
    """校验 labels，返回 (decls, rows)。

    decls:  [{node, name(原始), alias(原始), origin(key 元组), originAlias}]
    rows:   {origin key: {"name":..., "alias":...}}（同一 origin 只允许一行）
    一条 pair/cable/route 行的名签会同时挂到其两端端口；route 行的名签
    只参与别名图解析与规范名计数（不参与静态互斥归并，也不再作为
    跨路由串线的判定依据——串线只追溯独立信号源与路由归属）。
    """
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise SignalAuditError("labels 必须是 JSON 对象，如 {\"ports\": [...]}")

    solver = plan["solver"]
    devices = plan["devices"]
    pairs = plan["pairs"]
    routes = plan["routes"]
    valid_ports = set()
    for name, d in devices.items():
        for p in d["inputs"] + d["outputs"]:
            valid_ports.add(f"{name}.{p}")
    pair_by_id = {p["id"]: p for p in pairs}
    cable_by_id = {c["id"]: c for c in solver.cables}

    def take(row, ctx):
        if not isinstance(row, dict):
            raise SignalAuditError(f"{ctx}：标签项必须是 JSON 对象")
        name, alias = _text(row.get("name")), _text(row.get("alias"))
        if not name and not alias:
            return None, None
        return name, alias

    decls, rows = [], {}

    def register(kind, key, nodes, name, alias, port=None):
        origin = (kind, key)
        if origin in rows:
            raise SignalAuditError(f"{_origin_label(kind, key, port)} 重复填写标签")
        rows[origin] = {"name": name, "alias": alias}
        for node in nodes:
            decls.append({"node": node, "name": name, "alias": alias,
                          "origin": origin, "originAlias": alias})

    for i, row in enumerate(raw.get("ports") or []):
        name, alias = take(row, f"ports[{i}]")
        if not name and not alias:
            continue
        port = _text(row.get("port"))
        if not port:
            raise SignalAuditError(f"ports[{i}]：缺少 port（设备.端口）")
        if port not in valid_ports:
            raise SignalAuditError(f"ports[{i}]：未知端口 “{port}”")
        register("port", port, [port], name, alias, port=port)

    for i, row in enumerate(raw.get("pairs") or []):
        name, alias = take(row, f"pairs[{i}]")
        if not name and not alias:
            continue
        try:
            pid = int(row.get("id"))
        except (TypeError, ValueError):
            raise SignalAuditError(f"pairs[{i}]：id 必须是插孔对编号整数")
        pair = pair_by_id.get(pid)
        if pair is None:
            raise SignalAuditError(f"pairs[{i}]：插孔对 {pid} 不存在")
        nodes = [f"{pair['top']['device']}.{pair['top']['port']}"] if pair["top"] else []
        if pair["bottom"]:
            nodes.append(f"{pair['bottom']['device']}.{pair['bottom']['port']}")
        register("pair", pid, nodes, name, alias)

    for i, row in enumerate(raw.get("cables") or []):
        name, alias = take(row, f"cables[{i}]")
        if not name and not alias:
            continue
        try:
            cid = int(row.get("id"))
        except (TypeError, ValueError):
            raise SignalAuditError(f"cables[{i}]：id 必须是跳线编号整数")
        cable = cable_by_id.get(cid)
        if cable is None:
            raise SignalAuditError(
                f"cables[{i}]：跳线 #{cid} 不在当前规划的已安装跳线中")
        register("cable", cid, [cable["fromPort"], cable["toPort"]], name, alias)

    for i, row in enumerate(raw.get("routes") or []):
        name, alias = take(row, f"routes[{i}]")
        if not name and not alias:
            continue
        try:
            ridx = int(row.get("index"))
        except (TypeError, ValueError):
            raise SignalAuditError(f"routes[{i}]：index 必须是路由序号整数（从 0 起）")
        if not 0 <= ridx < len(routes):
            raise SignalAuditError(f"routes[{i}]：路由序号 {ridx} 超出范围")
        r = routes[ridx]
        nodes = [f"{r['source']['device']}.{r['source']['port']}",
                 f"{r['target']['device']}.{r['target']['port']}"]
        register("route", ridx, nodes, name, alias)

    return decls, rows


# ---- 别名图：规范名解析与别名环 ------------------------------------------


def build_alias_graph(decls):
    """构造 别名→规范名 有向图（同名忽略）。

    返回 graph: {alias: {canonical: [origin, ...]}}（记录每条声明的来源）。
    """
    graph = defaultdict(lambda: defaultdict(list))
    for d in decls:
        if d["name"] and d["alias"] and d["alias"] != d["name"]:
            graph[d["alias"]][d["name"]].append(d)
    return graph


def _tarjan_scc(nodes, succ):
    """迭代 Tarjan，返回 SCC 列表（每个 SCC 为节点集合）。"""
    index_of, low, stack, onstack = {}, {}, [], set()
    counter = [0]
    sccs = []

    for root in list(nodes):
        if root in index_of:
            continue
        work = [(root, iter(succ.get(root, ())))]
        index_of[root] = low[root] = counter[0]
        counter[0] += 1
        stack.append(root)
        onstack.add(root)
        while work:
            u, it = work[-1]
            advanced = False
            for v in it:
                if v not in index_of:
                    index_of[v] = low[v] = counter[0]
                    counter[0] += 1
                    stack.append(v)
                    onstack.add(v)
                    work.append((v, iter(succ.get(v, ()))))
                    advanced = True
                    break
                if v in onstack:
                    low[u] = min(low[u], index_of[v])
            if advanced:
                continue
            work.pop()
            if work:
                p = work[-1][0]
                low[p] = min(low[p], low[u])
            if low[u] == index_of[u]:
                comp = set()
                while True:
                    w = stack.pop()
                    onstack.discard(w)
                    comp.add(w)
                    if w == u:
                        break
                sccs.append(comp)
    return sccs


def _cycle_in(comp, succ):
    """在一个成环 SCC 中取出一条有向简单环（含自环）。"""
    comp = set(comp)
    for s in comp:
        if s in succ.get(s, ()):
            return [s, s]
    color, parent = {}, {}
    for root in sorted(comp):
        if color.get(root, 0) != 0:
            continue
        color[root] = 1
        stack = [(root, iter(sorted(succ.get(root, ()))))]
        while stack:
            u, it = stack[-1]
            advanced = False
            for v in it:
                if v not in comp or color.get(v, 0) == 2:
                    continue
                if color.get(v, 0) == 1:              # 回到在栈祖先 → 回边
                    cyc, x = [u], u
                    while x != v:
                        x = parent[x]
                        cyc.append(x)
                    cyc.reverse()
                    cyc.append(v)
                    return cyc
                color[v], parent[v] = 1, u
                stack.append((v, iter(sorted(succ.get(v, ())))))
                advanced = True
                break
            if not advanced:
                color[u] = 2
                stack.pop()
    first = min(comp)
    return sorted(comp) + [first]


class NameResolver:
    """把任意名签（规范名或别名）解析为稳定的“代表规范名”。

    - 别名环：环内取“被显式声明为规范名次数最多、并列字典序最小”的成员；
    - 其余名字沿唯一出边收敛到终点；分叉时各终点分别可达。
    """

    def __init__(self, graph, decls):
        self.graph = graph
        succ = {a: set(t) for a, t in graph.items()}
        self.succ = succ
        name_votes = defaultdict(int)
        for d in decls:
            if d["name"]:
                name_votes[d["name"]] += 1
        self.name_votes = name_votes

        self.ring_rep = {}
        self.ring_members = set()
        nodes = set(succ) | {v for ts in succ.values() for v in ts}
        for comp in _tarjan_scc(nodes, succ):
            cyclic = len(comp) > 1 or any(u in succ.get(u, ()) for u in comp)
            if cyclic:
                rep = min(comp, key=lambda n: (-name_votes.get(n, 0), n))
                for n in comp:
                    self.ring_rep[n] = rep
                    self.ring_members.add(n)
        self._cache = {}

    def _resolve(self, name):
        if name in self.ring_rep:
            return self.ring_rep[name]
        seen = set()
        u = name
        while u in self.succ and u not in self.ring_members:
            if u in seen:                      # 理论上不会（非环节点无环）
                return u
            seen.add(u)
            targets = self.succ[u]
            if not targets:
                return u
            u = sorted(targets)[0]            # 分叉时取稳定值；分叉另有问题上报
        return self.ring_rep.get(u, u)

    def resolve(self, name):
        if name is None:
            return None
        if name not in self._cache:
            self._cache[name] = self._resolve(name)
        return self._cache[name]

    def canonicals_of_alias(self, alias):
        """别名沿有向边可达的全部代表规范名（供纯别名端点名采用）。"""
        out = set()
        stack = [alias]
        seen = set()
        while stack:
            u = stack.pop()
            if u in seen:
                continue
            seen.add(u)
            targets = self.succ.get(u)
            if not targets:
                out.add(self.resolve(u))
            else:
                stack.extend(targets)
        return out


# ---- 物理图（无向，规划完成后） -------------------------------------------


def build_physical_graph(plan):
    """设备内部 + 生效常态 + 已安装跳线的无向图。

    返回 adj: {port: [(neighbor, edge), ...]}；edge 为序列化友好的描述。
    """
    solver = plan["solver"]
    pairs = plan["pairs"]
    adj = defaultdict(list)

    def link(a, b, edge):
        adj[a].append((b, edge))
        adj[b].append((a, edge))

    for name, d in plan["devices"].items():
        for i in d["inputs"]:
            for o in d["outputs"]:
                link(f"{name}.{i}", f"{name}.{o}",
                     {"type": "device", "label": f"{name} 内部", "device": name})
    for p in pairs:
        if solver.normal_active(p) and p["top"] and p["bottom"]:
            a = f"{p['top']['device']}.{p['top']['port']}"
            b = f"{p['bottom']['device']}.{p['bottom']['port']}"
            link(a, b, {"type": "normal", "pairId": p["id"],
                        "label": f"{MODE_LABELS[p['mode']]} · 第{p['id']}对"})
    for c in solver.cables:
        link(c["fromPort"], c["toPort"],
             {"type": "cable", "cableId": c["id"], "label": f"跳线 #{c['id']}"})

    # 确保孤立端口也是图节点
    for port in solver.port_dir:
        adj.setdefault(port, [])
    return adj


def build_signal_digraph(plan):
    """有向信号流图：设备内部 in→out、生效常态 上→下、跳线 from→to。

    与 solver.detect_loops 使用同一组有向边，用于追溯路由信号源：
    先按强连通分量合并互达的源端口（反馈环），再沿有向边**反向**追溯到
    入度为 0 的终源 SCC——共享同一终源的多个源端口只是同源分支，不构成
    跨路由串线（见 trace_signal_origins）。
    返回 succ: {port: {port, ...}}（含全部端口节点）。
    """
    solver = plan["solver"]
    succ = defaultdict(set)
    for name, d in plan["devices"].items():
        for i in d["inputs"]:
            for o in d["outputs"]:
                succ[f"{name}.{i}"].add(f"{name}.{o}")
    for p in plan["pairs"]:
        if solver.normal_active(p) and p["top"] and p["bottom"]:
            succ[f"{p['top']['device']}.{p['top']['port']}"].add(
                f"{p['bottom']['device']}.{p['bottom']['port']}")
    for c in solver.cables:
        succ[c["fromPort"]].add(c["toPort"])
    for port in solver.port_dir:
        succ.setdefault(port, set())
    return succ


def trace_signal_origins(succ, plan, sccs):
    """从每个 SCC 沿信号方向**反向追溯**上游终源（设备内部 / 常态 / 跳线）。

    缩点图（condensation）上的“终源 SCC”＝没有任何入边的 SCC：信号只能从
    这里起源、没有更上游可追。只含输入端口的终源 SCC 是“悬空输入”（背板
    没接线的设备输入），它本身不产生信号、不计入终源；因此一个源 SCC 追不
    到任何输出型终源时，意味着它挂在一条完全无驱动的悬空总线上，其归属由
    调用方按无向物理分量另行归组。

    返回：
      scc_of     端口 → SCC 编号
      origins_of SCC → frozenset(输出型终源 SCC 编号)（可空：无驱动总线）
    """
    scc_of = {p: i for i, comp in enumerate(sccs) for p in comp}
    comp_pred = [set() for _ in sccs]
    for u, outs in succ.items():
        for v in outs:
            if scc_of[u] != scc_of[v]:
                comp_pred[scc_of[v]].add(scc_of[u])

    port_dir = plan["solver"].port_dir

    origins_of = {}
    for cid in range(len(sccs)):
        seen = {cid}
        q = deque([cid])
        while q:
            u = q.popleft()
            for v in comp_pred[u]:
                if v not in seen:
                    seen.add(v)
                    q.append(v)
        origins_of[cid] = frozenset(
            a for a in seen
            if not comp_pred[a]
            and any(port_dir[p][1] == "out" for p in sccs[a]))
    return scc_of, origins_of


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


def shortest_path(adj, starts, goals):
    """多源 BFS：starts 中任一端点到 goals 中任一端点的最短无向路径。

    返回端口列表（长度 ≥1）。相邻端口之间的边由调用方经 BFS 前驱重建。
    起点 / 终点均按端口名排序入队，邻接按图插入序（设备、常态、跳线，均为
    配置稳定顺序）展开，因此同跳数的最短路径命中选择也是可复现的。
    """
    starts, goals = set(starts), set(goals)
    common = starts & goals
    if common:
        return [min(common)], {}
    start_list = sorted(starts)
    prev = {s: (None, None) for s in start_list}
    q = deque(start_list)
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
        return None, prev
    nodes = []
    x = hit
    while x is not None:
        nodes.append(x)
        x, _ = prev[x]
    nodes.reverse()
    return nodes, prev


def edge_between(adj, a, b):
    for v, edge in adj.get(a, ()):
        if v == b:
            return edge
    return None


# ---- 证据序列化 -----------------------------------------------------------


def evidence_items(nodes, adj, plan, node_names):
    """端口序列 → 前端可渲染的 port/edge 交替列表。"""
    solver = plan["solver"]
    items = []
    for i, node in enumerate(nodes):
        items.append({
            "kind": "port", "port": node,
            "jack": solver.port_jack.get(node),
            "names": sorted(node_names.get(node, ())),
        })
        if i + 1 < len(nodes):
            edge = edge_between(adj, node, nodes[i + 1])
            items.append({"kind": "edge", **(edge or {
                "type": "link", "label": "连通"})})
    return items


# ---- 主审计 ---------------------------------------------------------------


def audit_signals(config, labels_raw):
    """对当前规划做信号命名一致性审计，返回结构化报告（不修改任何路由）。"""
    result, plan = plan_all(config)
    solver = plan["solver"]
    pairs = plan["pairs"]

    decls, rows = parse_labels(labels_raw or {}, plan)
    graph = build_alias_graph(decls)
    resolver = NameResolver(graph, decls)
    adj = build_physical_graph(plan)

    ports = list(solver.port_dir)
    find, union = _union_find(ports)
    for u, nbrs in adj.items():
        for v, _ in nbrs:
            if u < v:                       # 每条无向边合并一次
                union(u, v)

    # 路由实际经过的端口 / 常态 / 跳线
    used_normals, used_cables = set(), set()
    routed_ports = set()
    for info in plan["planned"]:
        if not info["ok"]:
            continue
        used_normals |= info["normals"]
        used_cables |= set(info["cableIds"])
        for kind, payload in info["items"]:
            if kind == "port":
                routed_ports.add(payload)
    active_component = {find(p) for p in routed_ports}

    # ---- 名签按端口聚合（仅静态标签；路由行名签只参与别名图与计数） ----
    static_at = defaultdict(list)          # node -> [{canonical, raw, origin}]
    static_labeled = set()                 # 任何静态名签（含纯别名）所在端口
    labeled_nodes = set()
    for d in decls:
        labeled_nodes.add(d["node"])
        if d["origin"][0] != "route":
            if d["name"] or d["alias"]:
                static_labeled.add(d["node"])
        if d["name"] and d["origin"][0] != "route":
            static_at[d["node"]].append({
                "canonical": resolver.resolve(d["name"]),
                "raw": d["name"], "origin": d["origin"],
                "originAlias": d["originAlias"],
            })

    # 每端口所有静态别名沿别名图收敛到的规范名（补全传播）
    alias_resolve_at = defaultdict(set)
    for d in decls:
        if d["origin"][0] == "route" or not d["alias"]:
            continue
        for canon in resolver.canonicals_of_alias(d["alias"]):
            alias_resolve_at[d["node"]].add(canon)

    components = defaultdict(lambda: {"ports": [], "names": defaultdict(list)})
    for p in ports:
        r = find(p)
        components[r]["ports"].append(p)
        for nd in static_at.get(p, ()):
            components[r]["names"][nd["canonical"]].append((p, nd))
        # 纯别名端口（无显式规范名）：别名唯一收敛到某规范名时按该名补全传播；
        # 显式规范名与别名收敛目标不一致属于别名环/分叉类问题，这里不做隐式合并。
        declared = {nd["canonical"] for nd in static_at.get(p, ())}
        if not declared:
            for canon in alias_resolve_at.get(p, ()):
                components[r]["names"][canon]

    issues = []

    def origin_payload(origin):
        kind, key = origin
        return {"kind": kind,
                "port": key if kind == "port" else None,
                "id": key if kind in ("pair", "cable") else None,
                "index": key if kind == "route" else None,
                "label": _origin_label(kind, key, port=key if kind == "port" else None)}

    # ---- 1) 互斥名称：同一物理分量出现多个静态规范名（每分量一条） --------
    for root, comp in components.items():
        names = [n for n, entries in comp["names"].items() if entries]
        if len(names) <= 1:
            continue
        votes = {n: sum(1 for _ in entries)
                 for n, entries in comp["names"].items() if entries}
        preferred = min(names, key=lambda n: (-votes[n], n))
        losers = [n for n in names if n != preferred]

        # 全局最短证据：preferred 名签端口 → 任一落败名签端口
        starts = [p for p, _ in comp["names"][preferred]]
        goals = [p for n in losers for p, _ in comp["names"][n]]
        nodes, _ = shortest_path(adj, starts, goals)
        loser_set = set(losers)
        issues.append({
            "type": MUTEX, "severity": "error",
            "title": TYPE_TITLES[MUTEX],
            "message": f"同一连通分量上出现 {len(names)} 个互斥的规范信号名："
                       f"{('、'.join(f'“{n}”×{votes[n]}' for n in names))}；"
                       f"物理连通意味着这些端点等电位，只能保留一个规范名。"
                       f"建议采纳声明次数最多的 “{preferred}”，其余降级为别名。",
            "component": root,
            "names": sorted(names), "conflictingNames": sorted(loser_set),
            "preferredName": preferred,
            "suggestedName": preferred,
            "evidence": evidence_items(nodes or starts[:1], adj, plan,
                                       _node_label_map(decls)),
            "fixes": _rename_fixes(comp["names"], preferred, loser_set, rows),
        })

    # ---- 2) 别名环（含别名分叉到多个规范名） -----------------------------
    issues += _alias_issues(graph, resolver, decls, adj, plan, rows)

    # ---- 3) 无标签断点：承载路由信号的跳线 / 半常态分流点 ----------------
    component_canonicals = {}
    for root, comp in components.items():
        present = {n for n, e in comp["names"].items() if e}
        component_canonicals[root] = present

    def suggested_for(root):
        present = component_canonicals.get(root, set())
        if len(present) == 1:
            return next(iter(present))
        return None

    # 3a. 已被路由使用、但信号流入端没有任何名签的跳线（无标签断点）。
    # 注：不要求跳线两端都命名——很多跳线的作用就是把已命名信号送入一个
    # 未命名输入（半常态并送/监听），只要“从哪根线流进来”可追踪即可。
    for cid in sorted(used_cables):
        cable = next(c for c in solver.cables if c["id"] == cid)
        a, b = cable["fromPort"], cable["toPort"]
        if ("cable", cid) in rows or a in static_labeled:
            continue
        nodes, _ = shortest_path(adj, [a], [b])
        root = find(a)
        suggested = suggested_for(root)
        issues.append({
            "type": UNLABELED, "severity": "warning",
            "title": TYPE_TITLES[UNLABELED],
            "message": f"跳线 #{cid}（{a} ↔ {b}）承载路由信号，但其自身与源端端口"
                       f"都没有规范名/别名，信号经跳线后无法继续追踪（无标签断点）。"
                       + (f" 分量内已有规范名 “{suggested}”，建议沿用。"
                          if suggested else " 分量内存在多个或零个规范名，需人工命名。"),
            "component": root, "names": sorted(component_canonicals.get(root, set())),
            "suggestedName": suggested,
            "evidence": evidence_items(nodes or [a, b], adj, plan, _node_label_map(decls)),
            "fixes": [{
                "action": "add-label", "target": origin_payload(("cable", cid)),
                "targetLabel": _origin_label("cable", cid),
                "name": suggested,
                "label": f"为跳线 #{cid} 补填规范名"
                         + (f" “{suggested}”" if suggested else "（需人工指定）"),
            }],
        })

    # 3b. 半常态分流点：上插孔插线 → 常态保持并送（且被路由使用）。
    # 只要源端口/插孔对/该跳线三者都无名签，分流出去的信号就无从命名。
    for p in sorted((x for x in pairs if x["mode"] not in (THROUGH,)),
                    key=lambda x: x["id"]):
        t_jack, b_jack = f"{p['id']}T", f"{p['id']}B"
        if not solver.normal_active(p) or t_jack not in solver.plugs:
            continue
        cid_at_t = solver.plugs[t_jack]
        if p["id"] not in used_normals and cid_at_t not in used_cables:
            continue
        t_port = f"{p['top']['device']}.{p['top']['port']}" if p["top"] else None
        b_port = (f"{p['bottom']['device']}.{p['bottom']['port']}"
                  if p["bottom"] else None)
        if ("pair", p["id"]) in rows \
                or ("cable", cid_at_t) in rows \
                or (t_port and t_port in static_labeled):
            continue
        starts = [t_port] if t_port else []
        goals = [b_port] if b_port else []
        nodes, _ = shortest_path(adj, starts, goals) if starts and goals else (None, {})
        root = find(t_port) if t_port else find(b_port)
        suggested = suggested_for(root)
        issues.append({
            "type": UNLABELED, "severity": "warning",
            "title": TYPE_TITLES[UNLABELED],
            "message": f"第 {p['id']} 对半常态插孔在上插孔 {t_jack} 插有跳线 "
                       f"#{cid_at_t}，常态保持、信号并送（分流/监听点），路由同时使用"
                       f"并送支路，但源端口、该插孔对与跳线都没有名签，分流出去的信号"
                       f"无法命名追踪（无标签断点）。"
                       + (f" 建议沿用分量内规范名 “{suggested}”。"
                          if suggested else " 分量内规范名不唯一，需先消歧再命名。"),
            "component": root, "names": sorted(component_canonicals.get(root, set())),
            "suggestedName": suggested,
            "evidence": evidence_items(nodes or [x for x in (t_port, b_port) if x],
                                       adj, plan, _node_label_map(decls)),
            "fixes": [{
                "action": "add-label", "target": origin_payload(("pair", p["id"])),
                "targetLabel": _origin_label("pair", p["id"]),
                "name": suggested,
                "label": f"为第 {p['id']} 对半常态分流点补填规范名"
                         + (f" “{suggested}”" if suggested else "（需人工指定）"),
            }],
        })

    # ---- 4) 跨路由串线：独立信号源的已规划路由汇于同一物理分量 ----------
    # 判定与名签（规范名/别名）完全解耦，只追溯信号源与路由归属：
    #  - 同一源端口的多条路由天然同源；
    #  - 源端口在有向信号流图上同属一个强连通分量（如反馈环两端）合并为一路；
    #  - 从每个路由源 SCC 沿信号方向**反向追溯**（设备内部 in→out 的反向、
    #    生效常态 上→下的反向、跳线 from→to 的反向）到全部“终源 SCC”
    #    （缩点图入度为 0、且含输出端口）：多个源 SCC 共享同一终源即同一
    #    真实独立信号的**同源分支**（典型：一个真实信号进入多输出设备后，
    #    从两个输出端口分别建路由；或被常态/跳线推动出的多条支路），归为
    #    一个来源，不增加串线计数；
    #  - 一个源 SCC 可同时由多个不同终源衍生（设备内部混合、半常态并送），
    #    其路由归属全部终源（下游混合点）；
    #  - 同一无向物理分量内出现两个不同终源 → 跨路由串线。改名/改别名不会
    #    改变归属，只有调整路由规划或物理连通才能消除，因此不提供 rename。
    #  - “只规划混合输出”时（如仅 MIX.out→MON.in 一条路由，两个真实源各自
    #    经生效常态进入 MIX.in1/in2），路由源 SCC 只命中 mix 一类，配对阶段
    #    会漏掉它。审计此时必须从混合点的终源集合**反向恢复**真实独立源：
    #    为每个终源补一个独立源组，把混合输出路由同时归属这些恢复出的源，
    #    再按“同一分量内不同终源两两配对”照常产出一条串线（证据端点取终源
    #    输出端口，因为真实源处并没有直接路由源端口可用）。
    dsucc = build_signal_digraph(plan)
    planned_routes = [(info["index"], info["source"])
                      for info in plan["planned"] if info["ok"]]
    source_ports = {src for _, src in planned_routes}

    sccs = _tarjan_scc(dsucc.keys(), dsucc)
    scc_of, origins_of = trace_signal_origins(dsucc, plan, sccs)
    source_sccs = {scc_of[p] for p in source_ports}

    routes_from = defaultdict(list)          # 源端口 → 从该端口出发的路由
    for idx, src in planned_routes:
        routes_from[src].append(idx)

    def scc_ports(cid):
        """SCC 内的源端口（仅路由源），字典序。"""
        return sorted(p for p in source_ports if scc_of[p] == cid)

    def origin_ports(cid):
        """终源 SCC 内的全部输出端口（字典序），即便那里没有直接建路由。"""
        return sorted(p for p in sccs[cid] if solver.port_dir[p][1] == "out")

    # 每个源 SCC 的“来源键”：
    #  唯一终源      → ("origin", frozenset({该终源}))：该独立源的一个分支；
    #  多个不同终源  → ("mix", frozenset(终源集))：真正的多源混合点；
    #  追不到任何输出终源（无驱动设备总线）→ ("dead", frozenset({物理分量根}))：
    #     没有任何真实信号源可追，同一无向物理分量内的无驱动分支同属一条
    #     悬空（等电位）总线，归为同一个未接线来源；不同分量的悬空总线
    #     互不相同。
    def identity_of(cid, root):
        outs = origins_of[cid]
        if len(outs) == 1:
            return ("origin", outs)
        if len(outs) >= 2:
            return ("mix", outs)
        return ("dead", frozenset((root,)))

    def origin_list(key):
        """来源键对应的终源输出端口（跨成员 SCC 聚合，字典序稳定）。"""
        if key[0] == "origin":
            return origin_ports(next(iter(key[1])))
        if key[0] == "mix":
            return sorted({p for o in key[1] for p in origin_ports(o)})
        return []                              # 无驱动总线：终源侧没有命名端口

    # 同源分支按“来源键”归组，且只在其路由源所在的**同一无向物理分量**内
    # 归并（有向衍生必在同一无向分量内；不同分量即便共用终源也不相关）。
    # group: {key, kind, origin, originPorts, members[(cid, ports)], routes}
    groups_by_comp = defaultdict(dict)
    mix_members = []                         # (物理分量, 组, 源 SCC)
    for cid in source_sccs:
        members = scc_ports(cid)
        root = find(members[0]) if members else None
        key = identity_of(cid, root)
        g = groups_by_comp[root].get(key)
        if g is None:
            kind = key[0]
            origin = cid if kind == "origin" else None
            g = {"key": key, "kind": kind, "origin": origin,
                 "originPorts": origin_list(key),
                 "members": [], "routes": []}
            groups_by_comp[root][key] = g
        else:
            # 不同 SCC 可能恰有相同来源键（如混合点经不同路径并流）：
            # 终源端口仍以来源键为准聚合，避免漏掉后加入成员携带的终源。
            g["originPorts"] = origin_list(key)
        g["members"].append((cid, members))
        g["routes"].extend(i for p in members for i in routes_from[p])
        if g["kind"] == "mix":
            mix_members.append((root, g, cid))
    for groups in groups_by_comp.values():
        for g in groups.values():
            g["members"].sort(key=lambda m: (m[1] or [""])[0])
            g["routes"] = sorted(set(g["routes"]))

    # 从每个物理分量内的混合组（mix）**反向恢复真实独立源**：混合点自身没有
    # 路由源 SCC 以外的独立组时（典型：只规划了混合输出 MIX.out→MON.in，
    # 没有任何一条路由直接从两个真实源出发），若不补源，配对阶段只会看到
    # 单个 mix 组而漏报串线。因此对 mix 来源键中的每个终源 SCC：
    #   - 该终源在本分量已有独立源组 → 把混合点的路由补归属到该组（混合
    #     输出同时携带该独立源的信号）；
    #   - 否则合成一个“恢复独立源”虚拟组（无实际路由源 SCC，members 为空，
    #     代表端口取终源输出端口），让混合点的每两个不同终源仍能配成一对
    #     串线；这些路由在 payload 中同时归属恢复出的两源。
    for uroot, gmap in groups_by_comp.items():
        recovered = {}              # 终源 SCC → 恢复/既有的独立源组
        for g in gmap.values():
            if g["kind"] == "origin" and g["origin"] is not None:
                recovered[g["origin"]] = g
        # 混合组按代表端口稳定遍历，保证补归属与虚拟组顺序可复现
        for g in sorted((x for x in gmap.values() if x["kind"] == "mix"),
                        key=lambda x: x["originPorts"]):
            for ocid in sorted(g["key"][1], key=lambda c: origin_ports(c)):
                og = recovered.get(ocid)
                if og is None:
                    vkey = ("origin", frozenset((ocid,)))
                    og = gmap.get(vkey)
                    if og is None:
                        og = {"key": vkey, "kind": "origin", "origin": ocid,
                              "originPorts": origin_ports(ocid),
                              "members": [], "routes": [],
                              "recoveredFromMix": True}
                        gmap[vkey] = og
                    recovered[ocid] = og
                og["routes"] = sorted(set(og["routes"]) | set(g["routes"]))

    def group_payload(g):
        # 代表端口：独立源取反向追溯到的终源输出（稳定的来源归属），
        # 混合点 / 无驱动总线取其实际路由源端口（混合就发生在那里）。
        # 从混合点恢复出的虚拟独立源没有路由源端口（members 为空），
        # 其代表与证据端点直接取反向追溯到的终源输出端口。
        member_ports = sorted({p for _, mp in g["members"] for p in mp})
        if g["kind"] == "origin":
            rep = (g["originPorts"][0] if g["originPorts"] else "")
            ports = member_ports or list(g["originPorts"])
        else:
            rep = member_ports[0] if member_ports else (
                g["originPorts"][0] if g["originPorts"] else "")
            ports = member_ports
        return {"port": rep, "ports": ports,
                "originPorts": g["originPorts"],
                "routes": g["routes"],
                "mixed": g["kind"] == "mix"}

    def desc_group(g):
        gp = group_payload(g)
        rts = "、".join(str(i + 1) for i in gp["routes"])
        ports = "、".join(gp["ports"])
        head = f"源端口 {ports} 的路由 {rts}" if rts else f"源端口 {ports}"
        if g.get("recoveredFromMix"):
            # 只规划了混合输出时，真实源没有任何直接路由：这里的归属关系是
            # 从混合点沿信号方向反向恢复出来的。
            head = f"{head}（真实源无直接路由，由下游混合输出反向恢复归属）"
        elif g["kind"] == "origin" and g["originPorts"] \
                and g["originPorts"] != gp["ports"]:
            up = "、".join(g["originPorts"])
            head = f"{head}（反向追溯到共同上游 {up}，为同源分支）"
        return head

    # 独立源（非混合）组按物理分量两两配对；混合点同时归属多个独立源时，
    # 作为下游混合点附注到对应串线条目（与旧逻辑的 downstream 语义一致）。
    for uroot, gmap in groups_by_comp.items():
        independents = sorted((g for g in gmap.values() if g["kind"] != "mix"),
                              key=lambda g: group_payload(g)["port"])
        for ai in range(len(independents)):
            for bi in range(ai + 1, len(independents)):
                ga, gb = independents[ai], independents[bi]
                pa, pb = group_payload(ga), group_payload(gb)
                # 证据：两组来源代表端口之间的最短无向物理路径
                # （常规独立源组为实际路由源端口；由混合点反向恢复出的组取
                # 终源输出端口——其真实源处并没有直接路由源端口）
                nodes, _ = shortest_path(adj, pa["ports"], pb["ports"])
                if nodes is None:
                    continue
                # 下游混合点：混合组的终源集合同时包含这两个独立源
                a_ids, b_ids = ga["key"][1], gb["key"][1]
                a_origin = next(iter(a_ids))
                b_origin = next(iter(b_ids))
                downstream = []
                for root, g, cid in mix_members:
                    if root != uroot:
                        continue
                    ids = g["key"][1]
                    if a_origin in ids and b_origin in ids and g["routes"]:
                        downstream.append(group_payload(g))
                downstream.sort(key=lambda x: x["port"])
                rts_all = sorted(set(pa["routes"] + pb["routes"]
                                     + [i for g in downstream for i in g["routes"]]))
                tail = ""
                if downstream:
                    tail = ("；下游混合点 " + "、".join(
                        f"源端口 {g['ports'][0]}（路由 "
                        f"{'、'.join(str(i + 1) for i in g['routes'])}）"
                        for g in downstream)
                        + " 的信号由上述独立源并流而成，这些路由同时归属两个独立源")
                issues.append({
                    "type": CROSSTALK, "severity": "error",
                    "title": TYPE_TITLES[CROSSTALK],
                    "message": f"独立信号源「{desc_group(ga)}」与「{desc_group(gb)}」"
                               f"的已规划路由在同一物理连通分量汇合，两个反向追溯到"
                               f"不同终源、互不衍生的独立信号在该分量内等电位（覆盖"
                               f"设备内部混合与半常态并送汇合），发生跨路由串线。"
                               f"改名或改别名不会改变信号源归属，无法消除本报警{tail}。"
                               f"只能调整路由规划（取消或改道相关路由）或断开它们"
                               f"之间的物理连通（拔除跳线 / 改变常态模式 / 修改背板"
                               f"接线）后重新规划。",
                    "resolution": "调整路由规划或断开独立源之间的物理连通后重新规划；"
                                  "重命名规范名 / 别名不能消除串线。",
                    "component": uroot,
                    # names 沿用数组结构供排序使用，内容为与标签无关的源端口
                    "names": sorted(pa["ports"] + pb["ports"]),
                    "sources": [{"port": pa["port"], "ports": pa["ports"],
                                 "originPorts": pa["originPorts"],
                                 "routes": pa["routes"]},
                                {"port": pb["port"], "ports": pb["ports"],
                                 "originPorts": pb["originPorts"],
                                 "routes": pb["routes"]}],
                    "routes": rts_all,
                    "routesBySource": {pa["port"]: pa["routes"],
                                       pb["port"]: pb["routes"]},
                    "downstream": [{"port": g["port"], "ports": g["ports"],
                                    "originPorts": g["originPorts"],
                                    "routes": g["routes"]} for g in downstream],
                    "evidence": evidence_items(nodes, adj, plan,
                                               _node_label_map(decls)),
                    "fixes": [],
                })

    # ---- 排序与编号 ------------------------------------------------------
    order = {MUTEX: 0, CROSSTALK: 1, ALIAS_RING: 2, UNLABELED: 3}
    issues.sort(key=lambda x: (0 if x["severity"] == "error" else 1,
                               order[x["type"]], x["type"],
                               min(x.get("names") or [""])))
    total_counts = {MUTEX: 0, ALIAS_RING: 0, UNLABELED: 0, CROSSTALK: 0}
    for iss in issues:
        total_counts[iss["type"]] += 1
    issue_total_full = len(issues)
    truncated = issue_total_full > ISSUE_LIMIT
    issues = issues[:ISSUE_LIMIT]
    for i, iss in enumerate(issues):
        iss["id"] = i

    # ---- 分量摘要（前端按分量着色） --------------------------------------
    comp_index, comp_out = {}, []
    for root in sorted(components, key=lambda r: (r not in active_component,
                                                  min(components[r]["ports"]))):
        ci = len(comp_out)
        comp_index[root] = ci
        ports_sorted = sorted(components[root]["ports"])
        comp_out.append({
            "id": ci,
            "active": root in active_component,
            "ports": [{"port": p, "jack": solver.port_jack.get(p),
                       "labeled": p in labeled_nodes}
                      for p in ports_sorted],
            "canonicalNames": sorted(n for n, e in components[root]["names"].items() if e),
        })
    for iss in issues:
        iss["component"] = comp_index.get(iss["component"])

    # mutex / alias-ring / crosstalk 均为 error 级，unlabeled 为 warning
    has_error = bool(total_counts[MUTEX] + total_counts[ALIAS_RING]
                     + total_counts[CROSSTALK])

    return {
        "ok": not has_error,
        "planOk": result["ok"],
        "summary": {
            "components": len(comp_out),
            "activeComponents": len(active_component),
            "labeledPorts": len(labeled_nodes),
            "issueCounts": total_counts,
            "issueTotal": issue_total_full,
            "issueShown": len(issues),
            "truncated": truncated,
        },
        "components": comp_out,
        "issues": issues,
        "pairs": result["pairs"],
        "cables": result["cables"],
        "routes": [{
            "index": i,
            "source": f"{r['source']['device']}.{r['source']['port']}",
            "target": f"{r['target']['device']}.{r['target']['port']}",
            "status": ("ok" if plan["planned"][i]["ok"] else "conflict"),
        } for i, r in enumerate(plan["routes"])],
        "auditNote": ("审计只读取规划结果传播名签，不修改任何路由、常态与跳线。"
                      + (f" 问题超过 {ISSUE_LIMIT} 条，仅展示前 {ISSUE_LIMIT} 条。"
                         if truncated else "")),
    }


# ---- 修正预览 -------------------------------------------------------------


def _rename_fix(origin, raw_name, preferred, rows):
    old_alias = (rows.get(origin) or {}).get("alias")
    return {
        "action": "rename",
        "target": {
            "kind": origin[0],
            "port": origin[1] if origin[0] == "port" else None,
            "id": origin[1] if origin[0] in ("pair", "cable") else None,
            "index": origin[1] if origin[0] == "route" else None,
            "label": _origin_label(origin[0], origin[1],
                                   port=origin[1] if origin[0] == "port" else None),
        },
        "targetLabel": _origin_label(origin[0], origin[1],
                                     port=origin[1] if origin[0] == "port" else None),
        "fromName": raw_name, "toName": preferred,
        "name": preferred,
        "alias": raw_name if not old_alias else old_alias,
        "label": f"{_origin_label(origin[0], origin[1], port=origin[1] if origin[0] == 'port' else None)}："
                 f"“{raw_name}” 降级为别名，规范名改为 “{preferred}”",
    }


def _rename_fixes(names_map, preferred, loser_set, rows):
    """把分量内所有落败规范名的声明行整理成去重后的 rename 预览。"""
    fixes, seen = [], set()
    for canon, entries in names_map.items():
        if canon not in loser_set or canon == preferred:
            continue
        for _, nd in entries:
            if nd["origin"] in seen:
                continue
            seen.add(nd["origin"])
            fixes.append(_rename_fix(nd["origin"], nd["raw"], preferred, rows))
    return fixes


def _target_of(origin):
    return {
        "kind": origin[0],
        "port": origin[1] if origin[0] == "port" else None,
        "id": origin[1] if origin[0] in ("pair", "cable") else None,
        "index": origin[1] if origin[0] == "route" else None,
        "label": _origin_label(origin[0], origin[1],
                               port=origin[1] if origin[0] == "port" else None),
    }


def _break_alias_fix(origin, alias, preferred, rows):
    """剪断别名环边：删除该行的别名（保留/统一规范名）。"""
    row = rows.get(origin) or {}
    return {
        "action": "break-alias",
        "target": _target_of(origin),
        "targetLabel": _origin_label(origin[0], origin[1],
                                     port=origin[1] if origin[0] == "port" else None),
        "removedAlias": alias,
        "name": preferred,
        "alias": None,
        "label": f"{_origin_label(origin[0], origin[1], port=origin[1] if origin[0] == 'port' else None)}："
                 f"删除别名 “{alias}”"
                 + (" 以剪断别名环" if (row.get("name") or "") == preferred
                    else f"，规范名改为 “{preferred}”（原 “{row.get('name')}” 降级为别名）"),
    }


# ---- 别名环问题 -----------------------------------------------------------


def _node_label_map(decls):
    m = defaultdict(set)
    for d in decls:
        if d["name"]:
            m[d["node"]].add(d["name"])
        if d["alias"]:
            m[d["node"]].add(d["alias"])
    return m


def _alias_issues(graph, resolver, decls, adj, plan, rows):
    issues = []

    # 2a. 同一别名被声明指向多个不同规范名（分叉）
    for alias in sorted(graph):
        targets = sorted(graph[alias])
        if len(targets) <= 1:
            continue
        votes = {t: resolver.name_votes.get(t, 0) for t in targets}
        preferred = min(targets, key=lambda t: (-votes[t], t))
        fixes, seen = [], set()
        for t in targets:
            if t == preferred:
                continue
            for d in graph[alias][t]:
                if d["origin"] in seen:
                    continue
                seen.add(d["origin"])
                fx = _rename_fix(d["origin"], d["name"], preferred, rows)
                fx["alias"] = alias                 # 保留原别名指向
                fixes.append(fx)
        # 证据：两个分叉声明所在端口之间的最短路径（找不到则只列端点）
        ports_a = [d["node"] for d in graph[alias][targets[0]]]
        ports_b = [d["node"] for d in graph[alias][targets[1]]]
        nodes, _ = shortest_path(adj, ports_a, ports_b)
        trail = " → ".join(targets)
        issues.append({
            "type": ALIAS_RING, "severity": "error",
            "title": TYPE_TITLES[ALIAS_RING],
            "subtype": "diverging",
            "message": f"别名 “{alias}” 同时被声明为 {len(targets)} 个不同规范名的别名"
                       f"（{trail}），指向分叉，无法确定它代表哪条信号。",
            "component": None, "names": [alias] + targets,
            "suggestedName": preferred,
            "evidence": evidence_items(nodes or ports_a[:1] + ports_b[:1],
                                       adj, plan, _node_label_map(decls)),
            "fixes": fixes,
        })

    # 2b. 有向环
    rings_done = set()
    for member in sorted(resolver.ring_members):
        rep = resolver.ring_rep[member]
        if rep in rings_done:
            continue
        rings_done.add(rep)
        comp = {n for n, r in resolver.ring_rep.items() if r == rep}
        cyc = _cycle_in(comp, resolver.succ)
        # 沿取出的环，保留指向采纳名 rep 的环边，其余环边逐边剪断
        fixes, seen = [], set()
        for i in range(len(cyc) - 1):
            alias_lead, canon_target = cyc[i], cyc[i + 1]
            decl_candidates = graph.get(alias_lead, {}).get(canon_target, [])
            for d in decl_candidates:
                if d["origin"] in seen:
                    continue
                seen.add(d["origin"])
                if canon_target == rep and (d.get("name") or "") == rep:
                    continue
                fixes.append(_break_alias_fix(d["origin"], alias_lead, rep, rows))
        # 证据：环上相邻名签的声明端口（取每跳最短路径拼接成本高，直接列出
        # 声明端口与环的文字链；端口都在时给一条最短连通路径作为物理证据）
        anchor_ports = [d["node"] for d in decls if d["name"] in comp or d["alias"] in comp]
        nodes = []
        if len(anchor_ports) >= 2:
            nodes, _ = shortest_path(adj, [anchor_ports[0]],
                                     [p for p in anchor_ports[1:] if p != anchor_ports[0]])
        issues.append({
            "type": ALIAS_RING, "severity": "error",
            "title": TYPE_TITLES[ALIAS_RING],
            "subtype": "cycle",
            "message": f"别名指向形成闭环：{' → '.join(cyc)}，没有任何一个名字是"
                       f"无争议的规范名，名签沿环传播会无限循环。建议采纳 “{rep}”"
                       f"（环内被显式声明为规范名次数最多）。",
            "component": None, "names": sorted(comp),
            "suggestedName": rep,
            "aliasChain": cyc,
            "evidence": evidence_items(nodes or anchor_ports[:2] or sorted(comp)[:1],
                                       adj, plan, _node_label_map(decls)),
            "fixes": fixes,
        })
    return issues
