"""录音棚跳线盘路由求解核心。

模型约定：
- 每台设备有若干输入/输出端口；设备内部信号从输入流向输出。
- 跳线盘由若干“插孔对”组成，每对含上插孔（T，背板接设备输出）与
  下插孔（B，背板接设备输入）。
- 常态（normalling）模式：
    full    全常态：上/下插孔任意一个插入插头都会断开常态连接；
    half    半常态：仅下插孔插入插头会断开常态；上插孔插入时常态保持，
            信号并送（可用于监听/分流）；
    through 直通：  上下插孔之间没有常态连接。
- 路由请求：从一个输出端口到一个输入端口。信号可经过常态连接（0 跳线）、
  设备内部通路（0 跳线）与前面板跳线（每条 1 根）。按用户给定顺序逐条
  求解，每条在当前插孔占用状态下用 0-1 最短路（Dijkstra）求最少跳线路径；
  已提交路由所占用的常态连接受保护，后续跳线不得将其断开。
- 全部路由提交后，在“设备内部通路 + 生效常态 + 跳线”构成的有向图上
  检测反馈环路。
"""

from heapq import heappop, heappush

FULL, HALF, THROUGH = "full", "half", "through"
MODES = (FULL, HALF, THROUGH)
MODE_LABELS = {FULL: "全常态", HALF: "半常态", THROUGH: "直通"}
INF = float("inf")


class ConfigError(ValueError):
    """配置非法。"""

    def __init__(self, message, code="CONFIG_INVALID", details=None):
        super().__init__(message)
        self.code = code
        self.details = details or {}


class FaultTargetError(ConfigError):
    """断路演练目标非法（跳线不存在、常态未生效等）。"""


def _pid(ref):
    return f"{ref['device']}.{ref['port']}"


def parse_config(config):
    """校验并解析用户配置，返回 (devices, pairs, routes, port_jack)。"""
    if not isinstance(config, dict):
        raise ConfigError("配置必须是 JSON 对象")

    devices = {}
    for d in config.get("devices") or []:
        name = str(d.get("name") or "").strip()
        if not name:
            raise ConfigError("设备名称不能为空")
        if "." in name:
            raise ConfigError(f"设备名不能包含点号: {name}")
        if name in devices:
            raise ConfigError(f"设备重复: {name}")
        ins = [str(p).strip() for p in (d.get("inputs") or []) if str(p).strip()]
        outs = [str(p).strip() for p in (d.get("outputs") or []) if str(p).strip()]
        for p in ins + outs:
            if "." in p:
                raise ConfigError(f"端口名不能包含点号: {p}")
        if len(set(ins)) != len(ins) or len(set(outs)) != len(outs):
            raise ConfigError(f"设备 {name} 存在重复端口")
        if set(ins) & set(outs):
            raise ConfigError(f"设备 {name} 的端口不能同时是输入和输出")
        if not ins and not outs:
            raise ConfigError(f"设备 {name} 至少需要一个端口")
        devices[name] = {"inputs": ins, "outputs": outs}
    if not devices:
        raise ConfigError("请至少定义一台设备")

    def port_ref(raw, want, ctx):
        if not isinstance(raw, str) or "." not in raw:
            raise ConfigError(f"{ctx}: 端口引用应为 “设备.端口”，得到 {raw!r}")
        dev, port = raw.split(".", 1)
        if dev not in devices:
            raise ConfigError(f"{ctx}: 未知设备 “{dev}”")
        pool = devices[dev]["outputs" if want == "out" else "inputs"]
        kind = "输出" if want == "out" else "输入"
        if port not in pool:
            raise ConfigError(f"{ctx}: 设备 {dev} 没有{kind}端口 “{port}”")
        return {"device": dev, "port": port, "dir": want}

    pairs = []
    seen_ids = set()
    port_jack = {}
    for i, p in enumerate(config.get("pairs") or [], 1):
        try:
            pid = int(p.get("id", i))
        except (TypeError, ValueError):
            raise ConfigError(f"第 {i} 对插孔：编号必须为整数")
        if pid in seen_ids:
            raise ConfigError(f"插孔对编号重复: {pid}")
        seen_ids.add(pid)
        mode = p.get("mode", THROUGH)
        if mode not in MODES:
            raise ConfigError(f"第 {pid} 对：模式必须是 full / half / through")
        top = port_ref(p["top"], "out", f"第 {pid} 对上插孔") if p.get("top") else None
        bottom = port_ref(p["bottom"], "in", f"第 {pid} 对下插孔") if p.get("bottom") else None
        for ref, jack in ((top, f"{pid}T"), (bottom, f"{pid}B")):
            if ref:
                k = _pid(ref)
                if k in port_jack:
                    raise ConfigError(f"端口 {k} 同时被插孔 {port_jack[k]} 和 {jack} 使用")
                port_jack[k] = jack
        pairs.append({"id": pid, "mode": mode, "top": top, "bottom": bottom})

    routes = []
    for i, r in enumerate(config.get("routes") or [], 1):
        src = port_ref(r.get("source"), "out", f"路由 {i} 源")
        dst = port_ref(r.get("target"), "in", f"路由 {i} 目标")
        routes.append({"source": src, "target": dst})
    if not routes:
        raise ConfigError("请至少定义一条路由")
    return devices, pairs, routes, port_jack


class Solver:
    def __init__(self, devices, pairs, routes, port_jack):
        self.devices = devices
        self.pairs = pairs
        self.routes = routes
        self.port_jack = port_jack                      # 端口 id -> 插孔 id
        self.jack_port = {j: p for p, j in port_jack.items()}
        self.plugs = {}                                 # 插孔 id -> 跳线编号
        self.cables = []                                # 已提交的跳线
        self.next_cid = 1                               # 单调递增的跳线编号
        self.protected_normals = set()                  # 被已提交路由占用的常态（插孔对编号）
        self.port_dir = {}
        for name, d in devices.items():
            for p in d["inputs"]:
                self.port_dir[f"{name}.{p}"] = (name, "in")
            for p in d["outputs"]:
                self.port_dir[f"{name}.{p}"] = (name, "out")
        self.pair_of_jack = {}
        for p in pairs:
            self.pair_of_jack[f"{p['id']}T"] = p
            self.pair_of_jack[f"{p['id']}B"] = p
        self.normal_from = {}                           # 上端口 id -> (下端口 id, pair)
        for p in pairs:
            if p["mode"] != THROUGH and p["top"] and p["bottom"]:
                self.normal_from[_pid(p["top"])] = (_pid(p["bottom"]), p)
        self.broken_normals = set()                     # 断路演练：强制失效的插孔对编号
        self.input_jacks = [(pid, j) for pid, j in port_jack.items()
                            if self.port_dir[pid][1] == "in"]

    # ---- 常态与插孔状态 -------------------------------------------------

    def normal_active(self, pair, extra_plugs=()):
        if pair["id"] in self.broken_normals:
            return False
        t, b = f"{pair['id']}T", f"{pair['id']}B"
        plugged = lambda j: j in self.plugs or j in extra_plugs
        if pair["mode"] == FULL:
            return not plugged(t) and not plugged(b)
        if pair["mode"] == HALF:
            return not plugged(b)
        return False

    def cable_forbidden(self, jack):
        """向 jack 插入插头是否会断开被已提交路由占用的常态连接。"""
        pair = self.pair_of_jack[jack]
        if pair["id"] not in self.protected_normals:
            return False
        if pair["mode"] == FULL:
            return True
        if pair["mode"] == HALF:
            return jack.endswith("B")
        return False

    # ---- 最短路 ---------------------------------------------------------

    def _neighbors(self, u, ignore_plugs):
        name, direction = self.port_dir[u]
        if direction == "in":
            for o in self.devices[name]["outputs"]:
                yield f"{name}.{o}", 0, ("device", name)
            return
        if u in self.normal_from:
            dst, pair = self.normal_from[u]
            if pair["id"] not in self.broken_normals and (ignore_plugs or self.normal_active(pair)):
                yield dst, 0, ("normal", pair)
        ju = self.port_jack.get(u)
        if ju and (ignore_plugs or (ju not in self.plugs and not self.cable_forbidden(ju))):
            for v, jv in self.input_jacks:
                if jv == ju:
                    continue
                if not ignore_plugs and (jv in self.plugs or self.cable_forbidden(jv)):
                    continue
                yield v, 1, ("cable", ju, jv)

    def find_path(self, src, dst, ignore_plugs=False):
        """0-1 最短路。返回 (跳线数, 路径项) 或 None。

        路径项为 ("port", 端口id) 与 ("edge", 边描述) 交替的列表。"""
        dist = {src: 0}
        prev = {}
        pq = [(0, src)]
        while pq:
            d, u = heappop(pq)
            if d > dist.get(u, INF):
                continue
            if u == dst:
                break
            for v, w, edge in self._neighbors(u, ignore_plugs):
                nd = d + w
                if nd < dist.get(v, INF):
                    dist[v] = nd
                    prev[v] = (u, edge)
                    heappush(pq, (nd, v))
        if dst not in dist:
            return None
        items = [("port", dst)]
        u = dst
        while u != src:
            pu, edge = prev[u]
            items.append(("edge", edge))
            items.append(("port", pu))
            u = pu
        items.reverse()
        return dist[dst], items

    def path_consistent(self, items):
        """路径自身的跳线不会断开路径自身依赖的常态。"""
        own = set()
        for kind, payload in items:
            if kind == "edge" and payload[0] == "cable":
                own.add(payload[1])
                own.add(payload[2])
        for kind, payload in items:
            if kind == "edge" and payload[0] == "normal":
                if not self.normal_active(payload[1], extra_plugs=own):
                    return False
        return True

    @staticmethod
    def extract_usage(items):
        """提取一条路径使用的常态编号集合与跳线插孔对集合。"""
        normals, cable_pairs = set(), []
        for kind, payload in items:
            if kind != "edge":
                continue
            if payload[0] == "normal":
                normals.add(payload[1]["id"])
            elif payload[0] == "cable":
                cable_pairs.append((payload[1], payload[2]))
        return normals, cable_pairs

    def commit(self, items):
        """提交一条路径：登记跳线、占用插孔、保护用到的常态。返回 {(from,to): 跳线号}。"""
        cable_ids = {}
        for kind, payload in items:
            if kind != "edge":
                continue
            if payload[0] == "normal":
                self.protected_normals.add(payload[1]["id"])
            elif payload[0] == "cable":
                _, fj, tj = payload
                if fj not in self.plugs:
                    cid = self.next_cid
                    self.next_cid += 1
                    self.cables.append({
                        "id": cid, "fromJack": fj, "toJack": tj,
                        "fromPort": self.jack_port[fj], "toPort": self.jack_port[tj],
                    })
                    self.plugs[fj] = cid
                    self.plugs[tj] = cid
                cable_ids[(fj, tj)] = self.plugs[fj]
        return cable_ids

    def build_chain(self, items, cable_ids):
        chain = []
        for kind, payload in items:
            if kind == "port":
                chain.append({"kind": "port", "port": payload,
                              "jack": self.port_jack.get(payload)})
            elif payload[0] == "device":
                chain.append({"kind": "edge", "type": "device",
                              "label": f"{payload[1]} 内部"})
            elif payload[0] == "normal":
                p = payload[1]
                chain.append({"kind": "edge", "type": "normal",
                              "label": f"{MODE_LABELS[p['mode']]} · 第{p['id']}对"})
            else:
                cid = cable_ids[(payload[1], payload[2])]
                chain.append({"kind": "edge", "type": "cable",
                              "label": f"跳线 #{cid}", "cableId": cid})
        return chain


def diagnose(solver, src, dst, ideal, broken_normals=()):
    """路由失败时，对照“忽略占用”的理想路径找出被阻塞的边与冲突端口。"""
    reasons, ports = [], []

    def add_port(jack):
        p = {"jack": jack, "port": solver.jack_port.get(jack)}
        if p not in ports:
            ports.append(p)

    if ideal is None:
        if src not in solver.port_jack:
            reasons.append(f"源端口 {src} 未接入跳线盘背板")
        if dst not in solver.port_jack:
            reasons.append(f"目标端口 {dst} 未接入跳线盘背板")
        if not reasons:
            reasons.append("源与目标之间不存在可用通路")
        return reasons, ports

    _, items = ideal
    for kind, payload in items:
        if kind != "edge":
            continue
        if payload[0] == "normal":
            pair = payload[1]
            if pair["id"] in broken_normals:
                reasons.append(
                    f"常态连接 第{pair['id']}对（{MODE_LABELS[pair['mode']]}）"
                    f"已在断路演练中被切断（断点 {pair['id']}T / {pair['id']}B）")
                add_port(f"{pair['id']}T")
                add_port(f"{pair['id']}B")
            elif not solver.normal_active(pair):
                for jack in (f"{pair['id']}T", f"{pair['id']}B"):
                    if jack in solver.plugs:
                        reasons.append(
                            f"常态连接 第{pair['id']}对（{MODE_LABELS[pair['mode']]}）"
                            f"被跳线 #{solver.plugs[jack]}（插入 {jack}）断开")
                        add_port(jack)
        elif payload[0] == "cable":
            for jack in (payload[1], payload[2]):
                if jack in solver.plugs:
                    reasons.append(
                        f"插孔 {jack}（{solver.jack_port.get(jack)}）"
                        f"已被跳线 #{solver.plugs[jack]} 占用")
                    add_port(jack)
                elif solver.cable_forbidden(jack):
                    pair = solver.pair_of_jack[jack]
                    reasons.append(
                        f"插孔 {jack}（{solver.jack_port.get(jack)}）插入后会断开"
                        f"第 {pair['id']} 对被其他路由占用的常态连接")
                    add_port(jack)
    dedup = list(dict.fromkeys(reasons))
    return dedup or ["当前状态下无可行通路"], ports


def detect_loops(devices, pairs, cables, normal_active):
    """在 设备内部通路 + 生效常态 + 跳线 构成的有向图中检测反馈环路。"""
    adj = {}

    def add(a, b):
        adj.setdefault(a, [])
        if b not in adj[a]:
            adj[a].append(b)

    for name, d in devices.items():
        for i in d["inputs"]:
            for o in d["outputs"]:
                add(f"{name}.{i}", f"{name}.{o}")
    for p in pairs:
        if p["mode"] != THROUGH and p["top"] and p["bottom"] and normal_active(p):
            add(_pid(p["top"]), _pid(p["bottom"]))
    for c in cables:
        add(c["fromPort"], c["toPort"])

    loops, seen = [], set()
    color, stack = {}, []

    def dfs(u):
        color[u] = 1
        stack.append(u)
        for v in adj.get(u, ()):
            if color.get(v, 0) == 0:
                dfs(v)
            elif color[v] == 1:
                cyc = stack[stack.index(v):] + [v]
                key = tuple(sorted(set(cyc)))
                if key not in seen:
                    seen.add(key)
                    loops.append(cyc)
        stack.pop()
        color[u] = 2

    for u in list(adj):
        if color.get(u, 0) == 0:
            dfs(u)
    return loops[:8]


def plug_note(pair, jack, jack_port):
    """说明向 jack 插入插头对常态连接的影响。"""
    if pair["mode"] == THROUGH or not (pair["top"] and pair["bottom"]):
        return None
    t, b = _pid(pair["top"]), _pid(pair["bottom"])
    if pair["mode"] == FULL:
        return f"跳线插入 {jack}：断开第 {pair['id']} 对全常态连接（{t} → {b}）"
    if jack.endswith("T"):
        return f"跳线插入 {jack}：半常态上插孔，常态保持，{t} 的信号并送至 {b}（可监听/分流）"
    return f"跳线插入 {jack}：断开第 {pair['id']} 对半常态连接（{t} → {b}）"


def plan_all(config):
    """按当前配置完成一次规划。

    返回 (result, plan)：
    - result: 与原 solve() 兼容的输出（另含 failurePoints）
    - plan:   供断路演练复用的内部状态
    """
    devices, pairs, routes, port_jack = parse_config(config)
    s = Solver(devices, pairs, routes, port_jack)

    routes_out, conflicts = [], []
    planned = []                                     # 每条路由的提交信息（按 idx）
    for idx, r in enumerate(routes):
        src, dst = _pid(r["source"]), _pid(r["target"])
        inconsistent = False
        found = s.find_path(src, dst)
        if found and not s.path_consistent(found[1]):
            found = None
            inconsistent = True
        if not found:
            ideal = s.find_path(src, dst, ignore_plugs=True)
            reasons, ports = diagnose(s, src, dst, ideal, set())
            if inconsistent:
                reasons = ["可行路径的跳线会断开该路径自身依赖的常态连接"] + reasons
            conflicts.append({"route": idx, "source": src, "target": dst,
                              "reasons": reasons, "ports": ports})
            routes_out.append({"index": idx, "source": src, "target": dst,
                               "status": "conflict", "reasons": reasons})
            planned.append({"index": idx, "source": src, "target": dst,
                            "ok": False, "items": None})
            continue
        cost, items = found
        cable_ids = s.commit(items)
        normals, cable_pairs = Solver.extract_usage(items)
        used_cables = sorted(set(cable_ids.values()))
        routes_out.append({
            "index": idx, "source": src, "target": dst, "status": "ok",
            "cables": used_cables,
            "chain": s.build_chain(items, cable_ids),
        })
        planned.append({"index": idx, "source": src, "target": dst,
                        "ok": True, "items": items, "cableIds": used_cables,
                        "normals": normals, "cablePairs": cable_pairs})

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

    result = {
        "ok": not conflicts,
        "cableCount": len(s.cables),
        "cables": s.cables,
        "steps": steps,
        "notes": notes,
        "routes": routes_out,
        "conflicts": conflicts,
        "feedbackLoops": loops,
        "pairs": pairs_out,
        "failurePoints": failure_points,
    }
    plan = {
        "devices": devices, "pairs": pairs, "routes": routes,
        "port_jack": port_jack, "solver": s,
        "planned": planned, "conflicts": conflicts,
        "loops": loops, "pairs_out": pairs_out,
        "failure_points": failure_points,
        "orig_max_cable": max((c["id"] for c in s.cables), default=0),
    }
    return result, plan


def pairs_summary(pairs, solver):
    return [{
        "id": p["id"], "mode": p["mode"], "modeLabel": MODE_LABELS[p["mode"]],
        "topJack": f"{p['id']}T", "bottomJack": f"{p['id']}B",
        "topPort": _pid(p["top"]) if p["top"] else None,
        "bottomPort": _pid(p["bottom"]) if p["bottom"] else None,
        "normalActive": bool(p["mode"] != THROUGH and p["top"] and p["bottom"]
                             and solver.normal_active(p)),
    } for p in pairs]


def compute_failure_points(planned, solver):
    """枚举可断路的已安装跳线与生效常态，按受影响路由数降序排列。"""
    cable_routes, normal_routes = {}, {}
    for info in planned:
        if not info["ok"]:
            continue
        for cid in info["cableIds"]:
            cable_routes.setdefault(cid, []).append(info["index"])
        for nid in info["normals"]:
            normal_routes.setdefault(nid, []).append(info["index"])

    points = []
    for c in solver.cables:
        idxs = cable_routes.get(c["id"], [])
        points.append({
            "type": "cable", "id": c["id"],
            "label": f"跳线 #{c['id']}：{c['fromJack']}（{c['fromPort']}）"
                     f" ↔ {c['toJack']}（{c['toPort']}）",
            "affectedCount": len(idxs),
            "affectedRoutes": idxs,
            "ports": [
                {"jack": c["fromJack"], "port": c["fromPort"]},
                {"jack": c["toJack"], "port": c["toPort"]},
            ],
        })
    active_ids = {p["id"] for p in solver.pairs if solver.normal_active(p)}
    for nid in sorted(active_ids):
        idxs = normal_routes.get(nid, [])
        pair = solver.pair_of_jack[f"{nid}T"]
        points.append({
            "type": "normal", "id": nid,
            "label": f"常态连接 第{nid}对（{MODE_LABELS[pair['mode']]}）："
                     f"{_pid(pair['top'])} → {_pid(pair['bottom'])}",
            "affectedCount": len(idxs),
            "affectedRoutes": idxs,
            "ports": [
                {"jack": f"{nid}T", "port": _pid(pair["top"])},
                {"jack": f"{nid}B", "port": _pid(pair["bottom"])},
            ],
        })
    # 受影响多者在前；同数时按类型（跳线优先）与编号稳定排序
    points.sort(key=lambda p: (-p["affectedCount"], 0 if p["type"] == "cable" else 1, p["id"]))
    return points


def solve(config):
    result, _ = plan_all(config)
    return result


def _add_break_ports(acc, ports):
    for p in ports:
        if p not in acc:
            acc.append(dict(p))


def fault_assessment(config, target):
    """对指定的已安装跳线 / 生效常态做断路演练。

    在保持其他占用状态不变的前提下，按原路由顺序重新评估受影响路由，
    给出失效原因、断点端口与仍可用的替代路径。
    存在无替代路径的路由时抛 ConfigError(code="NO_ALTERNATIVE")，
    结构化结果挂在异常的 payload 上；非法目标抛 FaultTargetError。
    """
    if not isinstance(target, dict):
        raise FaultTargetError("断路目标必须是 JSON 对象 {type, id}",
                               code="FAULT_TARGET_INVALID")
    ttype = target.get("type")
    if ttype not in ("cable", "normal"):
        raise FaultTargetError("断路目标 type 必须是 cable（已安装跳线）或 normal（常态连接）",
                               code="FAULT_TARGET_INVALID")
    try:
        tid = int(target.get("id"))
    except (TypeError, ValueError):
        raise FaultTargetError("断路目标 id 必须是整数", code="FAULT_TARGET_INVALID")

    result, plan = plan_all(config)
    s = plan["solver"]
    planned = plan["planned"]
    orig_max = plan["orig_max_cable"]

    # ---- 定位断点并施加故障 --------------------------------------------
    target_point = next((fp for fp in plan["failure_points"]
                         if fp["type"] == ttype and fp["id"] == tid), None)
    if target_point is None:
        if ttype == "cable":
            msg = f"跳线 #{tid} 不存在：只能对规划结果中已安装的跳线断路"
        else:
            msg = (f"第 {tid} 对常态连接当前未生效（直通/未接线/已被插头断开）："
                   f"只能对生效中的常态连接断路")
        raise FaultTargetError(msg, code="FAULT_TARGET_INVALID",
                               details={"type": ttype, "id": tid,
                                        "available": plan["failure_points"]})

    break_ports = []
    _add_break_ports(break_ports, target_point["ports"])
    affected_set = set(target_point["affectedRoutes"])

    broken_cable = None
    if ttype == "cable":
        broken_cable = next(c for c in s.cables if c["id"] == tid)
        # 摘除故障跳线：释放其两端插孔（其余跳线占用保持不变）
        for jack in (broken_cable["fromJack"], broken_cable["toJack"]):
            s.plugs.pop(jack, None)
        s.cables = [c for c in s.cables if c["id"] != tid]
    else:
        s.broken_normals.add(tid)

    # 仅释放“全部使用方都是受影响路由”的常态保护；
    # 未受影响路由依赖的常态继续受保护，其他占用状态保持不变。
    keep_users = {}
    for info in planned:
        if not info["ok"] or info["index"] in affected_set:
            continue
        for nid in info["normals"]:
            keep_users.setdefault(nid, set()).add(info["index"])
    s.protected_normals &= set(keep_users)

    # ---- 按原顺序重新评估受影响路由 ------------------------------------
    restored, failed = [], []
    for info in sorted((p for p in planned if p["index"] in affected_set),
                       key=lambda p: p["index"]):
        idx, src, dst = info["index"], info["source"], info["target"]
        found = s.find_path(src, dst)
        if found and not s.path_consistent(found[1]):
            found = None
        if not found:
            ideal = s.find_path(src, dst, ignore_plugs=True)
            if ttype == "cable":
                lead = [f"原路径依赖的跳线 #{tid}（{broken_cable['fromJack']} "
                        f"{broken_cable['fromPort']} ↔ {broken_cable['toJack']} "
                        f"{broken_cable['toPort']}）已断路，且无可用替代跳线"]
            else:
                lead = [f"原路径依赖的第 {tid} 对常态连接已断路，且无可用替代通路"]
            reasons, ports = diagnose(s, src, dst, ideal, s.broken_normals)
            route_ports = list(break_ports)
            _add_break_ports(route_ports, ports)
            failed.append({
                "route": idx, "source": src, "target": dst,
                "status": "unroutable", "reasons": lead + reasons,
                "breakPorts": route_ports,
            })
            continue

        cost, items = found
        cable_ids = s.commit(items)
        chain = s.build_chain(items, cable_ids)
        used_ids = sorted(set(cable_ids.values()))
        new_ids = [cid for cid in used_ids if cid > orig_max]
        restored.append({
            "route": idx, "source": src, "target": dst,
            "status": "restored",
            "originalCables": info.get("cableIds", []),
            "newCables": new_ids,
            "extraCableCount": len(new_ids),
            "chain": chain,
            "breakPorts": list(break_ports),
        })

    # ---- 汇总结果 -------------------------------------------------------
    unaffected = [{
        "route": info["index"], "source": info["source"],
        "target": info["target"],
        "status": "unchanged" if info["ok"] else "baseline-conflict",
        "originalCables": info.get("cableIds", []),
    } for info in planned if info["index"] not in affected_set]

    new_cables = [dict(c) for c in s.cables if c["id"] > orig_max]
    steps = [
        f"替代跳线 #{c['id']}：一端插入 {c['fromJack']}（{c['fromPort']}），"
        f"另一端插入 {c['toJack']}（{c['toPort']}）"
        for c in new_cables
    ]
    loops = detect_loops(plan["devices"], plan["pairs"], s.cables, s.normal_active)

    # 排序：失效路由在前，其后按路由编号
    affected_out = sorted(
        failed + restored,
        key=lambda r: (0 if r["status"] == "unroutable" else 1, r["route"]))

    payload = {
        "ok": not failed,
        "target": {"type": ttype, "id": tid, "label": target_point["label"]},
        "affectedCount": len(affected_set),
        "affectedRoutes": affected_out,
        "unaffectedRoutes": unaffected,
        "breakPorts": break_ports,
        "newCables": new_cables,
        "newCableCount": len(new_cables),
        "steps": steps,
        "feedbackLoops": loops,
        "pairs": pairs_summary(plan["pairs"], s),
        "cables": [dict(c) for c in s.cables],
        "failurePoints": plan["failure_points"],
        "baselineOk": not plan["conflicts"],
        "baselineConflicts": [r["route"] for r in plan["conflicts"]],
    }
    if failed:
        err = ConfigError(
            f"断路演练完成，但 {len(failed)} 条路由无可用替代路径",
            code="NO_ALTERNATIVE", details=payload)
        err.payload = payload
        raise err
    return payload


EXAMPLES = {
    "standard": {
        "label": "标准录音链（可解）",
        "config": {
            "devices": [
                {"name": "MIC", "inputs": [], "outputs": ["out"]},
                {"name": "PRE", "inputs": ["in"], "outputs": ["out"]},
                {"name": "COMP", "inputs": ["in"], "outputs": ["out"]},
                {"name": "IF", "inputs": ["in1", "in2"], "outputs": ["out1", "out2"]},
                {"name": "REV", "inputs": ["in"], "outputs": ["out"]},
                {"name": "MON", "inputs": ["in"], "outputs": []},
            ],
            "pairs": [
                {"id": 1, "top": "MIC.out", "bottom": "PRE.in", "mode": "half"},
                {"id": 2, "top": "PRE.out", "bottom": "COMP.in", "mode": "half"},
                {"id": 3, "top": "COMP.out", "bottom": "IF.in1", "mode": "full"},
                {"id": 4, "top": "IF.out1", "bottom": "REV.in", "mode": "through"},
                {"id": 5, "top": "REV.out", "bottom": "IF.in2", "mode": "through"},
                {"id": 6, "top": "IF.out2", "bottom": "MON.in", "mode": "half"},
            ],
            "routes": [
                {"source": "MIC.out", "target": "IF.in1"},
                {"source": "MIC.out", "target": "MON.in"},
                {"source": "PRE.out", "target": "IF.in2"},
                {"source": "COMP.out", "target": "MON.in"},
                {"source": "MIC.out", "target": "REV.in"},
            ],
        },
    },
    "conflict": {
        "label": "冲突演示（无解，标明冲突端口）",
        "config": {
            "devices": [
                {"name": "MIC", "inputs": [], "outputs": ["out"]},
                {"name": "PRE", "inputs": ["in"], "outputs": ["out"]},
                {"name": "COMP", "inputs": ["in"], "outputs": ["out"]},
                {"name": "IF", "inputs": ["in1", "in2"], "outputs": ["out1", "out2"]},
                {"name": "REV", "inputs": ["in"], "outputs": ["out"]},
                {"name": "MON", "inputs": ["in"], "outputs": []},
            ],
            "pairs": [
                {"id": 1, "top": "MIC.out", "bottom": "PRE.in", "mode": "half"},
                {"id": 2, "top": "PRE.out", "bottom": "COMP.in", "mode": "half"},
                {"id": 3, "top": "COMP.out", "bottom": "IF.in1", "mode": "full"},
                {"id": 4, "top": "IF.out1", "bottom": "REV.in", "mode": "through"},
                {"id": 5, "top": "REV.out", "bottom": "IF.in2", "mode": "through"},
                {"id": 6, "top": "IF.out2", "bottom": "MON.in", "mode": "half"},
            ],
            "routes": [
                {"source": "MIC.out", "target": "IF.in1"},
                {"source": "REV.out", "target": "PRE.in"},
            ],
        },
    },
    "feedback": {
        "label": "反馈环路演示",
        "config": {
            "devices": [
                {"name": "PRE", "inputs": ["in"], "outputs": ["out"]},
                {"name": "COMP", "inputs": ["in"], "outputs": ["out"]},
                {"name": "IF", "inputs": ["in1"], "outputs": ["out1"]},
            ],
            "pairs": [
                {"id": 1, "top": "PRE.out", "bottom": "COMP.in", "mode": "through"},
                {"id": 2, "top": "COMP.out", "bottom": "IF.in1", "mode": "through"},
                {"id": 3, "top": "IF.out1", "bottom": "PRE.in", "mode": "through"},
            ],
            "routes": [
                {"source": "PRE.out", "target": "IF.in1"},
                {"source": "IF.out1", "target": "PRE.in"},
            ],
        },
    },
    "layout": {
        "label": "前面板走线排版演示（多分区 + 交叉优化）",
        "config": {
            "devices": [
                {"name": "MIC", "inputs": [], "outputs": ["out"]},
                {"name": "PRE", "inputs": ["in"], "outputs": ["out"]},
                {"name": "COMP", "inputs": [], "outputs": ["out"]},
                {"name": "EQ", "inputs": [], "outputs": ["out"]},
                {"name": "IF", "inputs": ["in1", "in2", "in3", "in4"],
                 "outputs": ["out1", "out2"]},
                {"name": "REV", "inputs": ["in"], "outputs": ["out"]},
                {"name": "MON", "inputs": ["in"], "outputs": []},
            ],
            "pairs": [
                {"id": 1, "top": "MIC.out", "bottom": "PRE.in", "mode": "through"},
                {"id": 2, "top": "PRE.out", "bottom": "IF.in1", "mode": "through"},
                {"id": 3, "top": "COMP.out", "bottom": "IF.in2", "mode": "through"},
                {"id": 4, "top": "EQ.out", "bottom": "IF.in3", "mode": "through"},
                {"id": 5, "top": "IF.out1", "bottom": "REV.in", "mode": "through"},
                {"id": 6, "top": "IF.out2", "bottom": "IF.in4", "mode": "through"},
                {"id": 7, "top": "REV.out", "bottom": "MON.in", "mode": "through"},
            ],
            "routes": [
                {"source": "MIC.out", "target": "IF.in3"},
                {"source": "PRE.out", "target": "IF.in4"},
                {"source": "COMP.out", "target": "REV.in"},
                {"source": "EQ.out", "target": "MON.in"},
                {"source": "IF.out1", "target": "PRE.in"},
            ],
            # 随示例一起载入“前面板走线排版”分区配置（parse_config 忽略未知键）
            "layout": {
                "zones": [
                    {"name": "左区", "columns": [1, 2, 3], "layers": [1, 2]},
                    {"name": "中区", "columns": [4, 5], "layers": [1, 2]},
                    {"name": "右区", "columns": [6, 7], "layers": [1, 2, 3]},
                ],
            },
        },
    },
    "rewire": {
        "label": "无中断改线演练演示（拆线/换接/补线，保护不中断）",
        "config": {
            "devices": [
                {"name": "MIC", "inputs": [], "outputs": ["out"]},
                {"name": "PRE", "inputs": ["in"], "outputs": ["out"]},
                {"name": "EQ", "inputs": ["in1", "in2"], "outputs": ["out"]},
                {"name": "CMP", "inputs": ["in"], "outputs": ["out"]},
                {"name": "AUX", "inputs": [], "outputs": ["out"]},
                {"name": "FX", "inputs": [], "outputs": ["out"]},
                {"name": "TAP", "inputs": [], "outputs": ["out"]},
                {"name": "MON", "inputs": ["in1", "in2", "in3"], "outputs": []},
                {"name": "REC", "inputs": ["in"], "outputs": []},
                {"name": "SND", "inputs": ["in"], "outputs": []},
            ],
            "pairs": [
                {"id": 1, "top": "MIC.out", "bottom": "PRE.in", "mode": "half"},
                {"id": 2, "top": "PRE.out", "bottom": "EQ.in1", "mode": "half"},
                {"id": 3, "top": "EQ.out", "bottom": "CMP.in", "mode": "half"},
                {"id": 4, "top": "CMP.out", "bottom": "MON.in1", "mode": "through"},
                {"id": 5, "top": "AUX.out", "bottom": "EQ.in2", "mode": "through"},
                {"id": 6, "top": None, "bottom": "MON.in2", "mode": "through"},
                {"id": 7, "top": None, "bottom": "REC.in", "mode": "through"},
                {"id": 8, "top": "FX.out", "bottom": None, "mode": "through"},
                {"id": 9, "top": None, "bottom": "MON.in3", "mode": "through"},
                {"id": 10, "top": "TAP.out", "bottom": "SND.in", "mode": "through"},
            ],
            "routes": [
                {"source": "MIC.out", "target": "MON.in1"},
                {"source": "CMP.out", "target": "REC.in"},
                {"source": "AUX.out", "target": "MON.in2"},
                {"source": "FX.out", "target": "EQ.in2"},
                {"source": "TAP.out", "target": "SND.in"},
            ],
        },
    },
    "audit": {
        "label": "命名审计演示（互斥名/别名环/无标签/串线）",
        "config": {
            "devices": [
                {"name": "MIC", "inputs": [], "outputs": ["out"]},
                {"name": "AUX", "inputs": [], "outputs": ["out"]},
                {"name": "PRE", "inputs": ["in"], "outputs": ["out"]},
                {"name": "COMP", "inputs": ["in"], "outputs": ["out"]},
                {"name": "MIX", "inputs": ["in1", "in2"], "outputs": ["out"]},
                {"name": "MON", "inputs": ["in"], "outputs": []},
                {"name": "SEND", "inputs": ["in"], "outputs": []},
            ],
            "pairs": [
                # 第1对半常态：上插孔插跳线并送（分流点），故意不命名 → 无标签断点
                {"id": 1, "top": "MIC.out", "bottom": "PRE.in", "mode": "half"},
                {"id": 2, "top": "PRE.out", "bottom": "COMP.in", "mode": "half"},
                {"id": 3, "top": "COMP.out", "bottom": "MIX.in1", "mode": "half"},
                # 第4对直通：AUX 信号跨线跳进 MIX.in2 → 与 MIC 信号在 MIX 内部汇合
                # （两个独立信号源并入同一物理分量 → 跨路由串线，改名不能消除）
                {"id": 4, "top": "AUX.out", "bottom": "MIX.in2", "mode": "through"},
                {"id": 5, "top": "MIX.out", "bottom": "MON.in", "mode": "half"},
                # 第6对直通：MIC.out 经 1T 跳线并送到 SEND（半常态上插孔插线）
                {"id": 6, "top": None, "bottom": "SEND.in", "mode": "through"},
            ],
            "routes": [
                {"source": "MIC.out", "target": "MIX.in1"},
                {"source": "MIC.out", "target": "COMP.in"},
                {"source": "AUX.out", "target": "MIX.in2"},
                {"source": "MIX.out", "target": "MON.in"},
                {"source": "MIC.out", "target": "SEND.in"},
            ],
            # 随示例一起载入的名签（parse_config 忽略未知键，审计接口才使用）
            "labels": {
                "ports": [
                    # MIC/PRE 链叫 VOX；两个别名声明首尾相接成环（VOX→LEAD→VOX）
                    {"port": "MIC.out", "name": "VOX", "alias": "LEAD"},
                    {"port": "PRE.out", "name": "LEAD", "alias": "VOX"},
                    # AUX 与 MIX 输出叫法不一致 → 互斥名；与 MIC 的串线由两个
                    # 独立信号源在同一分量汇合决定（不依赖这里的名字，改名也不消警）
                    {"port": "AUX.out", "name": "AUX-MIX"},
                    {"port": "MIX.out", "name": "MIX-OUT"},
                ],
                "pairs": [],
                # 规划后跳线 #1 = AUX.out(4T) → MIX.in2(4B)，故意错叫 WRONG
                "cables": [
                    {"id": 1, "name": "WRONG"},
                ],
                "routes": [
                    {"index": 0, "name": "VOX"},
                    {"index": 2, "name": "AUX-MIX"},
                ],
            },
        },
    },
    "isolation": {
        "label": "串音隔离演示（贪心冲突，回溯重排成功）",
        "config": {
            "devices": [
                {"name": "SRC", "inputs": [], "outputs": ["a", "b"]},
                {"name": "SW", "inputs": ["in1", "in2"], "outputs": ["out1", "out2"]},
                {"name": "M1", "inputs": ["in"], "outputs": []},
                {"name": "M2", "inputs": ["in"], "outputs": []},
            ],
            "pairs": [
                {"id": 1, "top": "SRC.a", "bottom": "SW.in1", "mode": "full"},
                {"id": 2, "top": "SRC.b", "bottom": "SW.in2", "mode": "half"},
                {"id": 3, "top": "SW.out1", "bottom": "M1.in", "mode": "full"},
                {"id": 4, "top": "SW.out2", "bottom": "M2.in", "mode": "through"},
            ],
            "routes": [
                # 路由 1（A 组）贪心走 0 跳线常态链穿过 SW；
                # 路由 2（B 组）的 0/1 跳线链路因此必然与 SW 合并 → 冲突。
                # 回溯重排：路由 1 改用跳线 1T→3B 断开两侧常态，
                # 路由 2 用 2T→4B，两组彻底分开，共 2 条跳线。
                {"source": "SRC.a", "target": "M1.in", "isolationGroup": "A"},
                {"source": "SRC.b", "target": "M2.in", "isolationGroup": "B"},
            ],
            "isolation": {
                "forbiddenPairs": [["A", "B"]],
            },
        },
    },
    "isolation-conflict": {
        "label": "串音隔离演示（强制共设备，无解证据）",
        "config": {
            "devices": [
                {"name": "S1", "inputs": [], "outputs": ["out"]},
                {"name": "S2", "inputs": [], "outputs": ["out"]},
                {"name": "MIX", "inputs": ["in1", "in2"], "outputs": ["out"]},
                {"name": "DST", "inputs": ["in"], "outputs": []},
            ],
            "pairs": [
                {"id": 1, "top": "S1.out", "bottom": "MIX.in1", "mode": "full"},
                {"id": 2, "top": "S2.out", "bottom": "MIX.in2", "mode": "full"},
                {"id": 3, "top": "MIX.out", "bottom": "DST.in", "mode": "full"},
            ],
            "routes": [
                # 两台信号源都要送到同一个 DST；跳线盘没有任何旁路插孔，
                # 两条链路无论如何规划都在 MIX 设备内部连通 → 禁配组 A/B
                # 永远共享物理连通分量。返回冲突组与 1 跳最短共享证据，
                # 不提交任何方案。
                {"source": "S1.out", "target": "DST.in", "isolationGroup": "A"},
                {"source": "S2.out", "target": "DST.in", "isolationGroup": "B"},
            ],
            "isolation": {
                "forbiddenPairs": [["A", "B"]],
            },
        },
    },
}
