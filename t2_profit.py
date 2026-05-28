#!/usr/bin/env python3
"""
EVE Online T2 制造利润计算器
用法:
  python3 t2_profit.py "425mm Railgun II" "Wasp II" "Nova Rage"
  python3 t2_profit.py --me 4 --runs 20 "425mm Railgun II"
  python3 t2_profit.py --no-invention "425mm Railgun II"  # 只算制造，不摊发明
  python3 t2_profit.py --system "Jita" "425mm Railgun II"  # Jita指数(默认Haajinen)

自动获取:
  - 蓝图材料 (fuzzwork API)
  - Jita 4-4 市场价格 (ESI)
  - 星系制造指数 (ESI)
  - 发明成功率/材料 (fuzzwork)
"""

import sys
import json
import math
import argparse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed

# ============================================================
# 常量
# ============================================================
DEFAULT_ME = 2
DEFAULT_RUNS = 10
BUILDING_TAX = 0.0025       # 0.25%
SCC_SURCHARGE = 0.04        # 4%
SALES_TAX = 0.04            # 4%
FORGE_REGION_ID = 10000002  # The Forge
JITA_STATION_ID = 60003760  # Jita 4-4

# 已知星系ID
SYSTEM_IDS = {
    "haajinen": 30001424,
    "jita": 30000142,
    "dodixie": 30002659,
    "rens": 30002510,
    "amarr": 30002187,
}

# ============================================================
# 缓存 (避免重复请求)
# ============================================================
_cache = {
    "industry_systems": None,
    "market_prices": {},
    "blueprint_data": {},
    "name_to_tid": {},
}


def _fetch_json(url, timeout=15):
    resp = urllib.request.urlopen(url, timeout=timeout)
    return json.loads(resp.read())


def _post_json(url, data, timeout=15):
    req = urllib.request.Request(url, json.dumps(data).encode(),
                                 headers={"Content-Type": "application/json"})
    resp = urllib.request.urlopen(req, timeout=timeout)
    return json.loads(resp.read())


# ============================================================
# 名称解析
# ============================================================
def resolve_names(names):
    """批量名称 → type_id"""
    result = {}
    to_lookup = []
    for name in names:
        if name in _cache["name_to_tid"]:
            result[name] = _cache["name_to_tid"][name]
        elif name.isdigit():
            result[name] = int(name)
            _cache["name_to_tid"][name] = int(name)
        else:
            to_lookup.append(name)

    if to_lookup:
        resp = _post_json(
            "https://esi.evetech.net/latest/universe/ids/?datasource=tranquility&language=en",
            to_lookup
        )
        for item in resp.get("inventory_types", []):
            _cache["name_to_tid"][item["name"]] = item["id"]
            result[item["name"]] = item["id"]

    return result


def tid_to_name(tid):
    """type_id → 名称"""
    for name, t in _cache["name_to_tid"].items():
        if t == tid:
            return name
    resp = _post_json(
        "https://esi.evetech.net/latest/universe/names/?datasource=tranquility",
        [tid]
    )
    for item in resp:
        if item["id"] == tid:
            return item["name"]
    return str(tid)


# ============================================================
# 蓝图数据
# ============================================================
def get_blueprint(product_tid):
    """获取T2蓝图数据 (制造材料 + 发明材料 + 概率)"""
    if product_tid in _cache["blueprint_data"]:
        return _cache["blueprint_data"][product_tid]

    # 找蓝图 type_id: 搜索 "{产品名} Blueprint"
    product_name = tid_to_name(product_tid)
    bp_name = f"{product_name} Blueprint"
    bp_ids = resolve_names([bp_name])
    bp_tid = bp_ids.get(bp_name)

    if not bp_tid:
        raise ValueError(f"找不到蓝图: {bp_name}")

    data = _fetch_json(f"https://www.fuzzwork.co.uk/blueprint/api/blueprint.php?typeid={bp_tid}")
    details = data.get("blueprintDetails", {})
    am = data.get("activityMaterials", {})

    result = {
        "bp_tid": bp_tid,
        "product_tid": product_tid,
        "product_name": product_name,
        "adjustedPrice": details.get("adjustedPrice", 0),
        "probability": details.get("probability", 0.34),
        "maxRuns": details.get("maxProductionLimit", 10),
        "productQty": details.get("productQuantity", 1),  # 每run产出数量(弹药类>1)
        "mfg_time": details.get("times", {}).get("1", 0),
        "materials": am.get("1", []),       # 制造材料
        "inv_materials": am.get("8", []),   # 发明材料
    }

    _cache["blueprint_data"][product_tid] = result
    return result


# ============================================================
# 星系制造指数
# ============================================================
def get_cost_index(system_name="haajinen"):
    """获取星系制造成本指数"""
    key = system_name.lower()
    sys_id = SYSTEM_IDS.get(key)
    if not sys_id:
        # 尝试按名称查找
        ids = resolve_names([system_name])
        sys_id = ids.get(system_name)
        if not sys_id:
            raise ValueError(f"未知星系: {system_name}")

    if _cache["industry_systems"] is None:
        _cache["industry_systems"] = _fetch_json(
            "https://esi.evetech.net/latest/industry/systems/?datasource=tranquility"
        )

    for s in _cache["industry_systems"]:
        if s["solar_system_id"] == sys_id:
            for ci in s["cost_indices"]:
                if ci["activity"] == "manufacturing":
                    return ci["cost_index"]

    raise ValueError(f"找不到 {system_name} 的制造指数")


# ============================================================
# 市场价格
# ============================================================
def get_market_prices(type_ids):
    """批量获取 Jita 4-4 买卖价"""
    result = {}
    to_fetch = []

    for tid in type_ids:
        if tid in _cache["market_prices"]:
            result[tid] = _cache["market_prices"][tid]
        else:
            to_fetch.append(tid)

    if not to_fetch:
        return result

    def fetch_one(tid):
        sell_min = None
        buy_max = None

        # 卖单
        for page in range(1, 4):
            try:
                url = f"https://esi.evetech.net/latest/markets/{FORGE_REGION_ID}/orders/?datasource=tranquility&order_type=sell&page={page}&type_id={tid}"
                orders = _fetch_json(url)
                if not orders:
                    break
                jita = [o['price'] for o in orders if o.get('location_id') == JITA_STATION_ID]
                if jita:
                    sell_min = min(jita)
                    break
                elif page == 1 and orders:
                    sell_min = min(o['price'] for o in orders)
            except:
                break

        # 买单
        for page in range(1, 3):
            try:
                url = f"https://esi.evetech.net/latest/markets/{FORGE_REGION_ID}/orders/?datasource=tranquility&order_type=buy&page={page}&type_id={tid}"
                orders = _fetch_json(url)
                if not orders:
                    break
                jita = [o['price'] for o in orders if o.get('location_id') == JITA_STATION_ID]
                if jita:
                    buy_max = max(jita)
                    break
                elif page == 1 and orders:
                    buy_max = max(o['price'] for o in orders)
            except:
                break

        return tid, {"sell_min": sell_min, "buy_max": buy_max}

    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(fetch_one, tid): tid for tid in to_fetch}
        for f in as_completed(futures):
            tid, prices = f.result()
            _cache["market_prices"][tid] = prices
            result[tid] = prices

    return result


# ============================================================
# 计算
# ============================================================
def calculate(product_name_or_tid, me=DEFAULT_ME, runs=DEFAULT_RUNS,
              system="haajinen", include_invention=True, buy_mode="sell"):
    """
    计算T2制造利润

    参数:
      product_name_or_tid: 产品名称或type_id
      me: 材料效率 (默认2)
      runs: 制造次数 (默认10)
      system: 制造星系 (默认haajinen)
      include_invention: 是否摊入发明成本 (默认True)
      buy_mode: "sell"=按卖价买材料, "buy"=按买单收材料

    返回: dict 包含完整计算结果
    """
    # 解析名称
    if isinstance(product_name_or_tid, int) or product_name_or_tid.isdigit():
        product_tid = int(product_name_or_tid)
        product_name = tid_to_name(product_tid)
    else:
        product_name = product_name_or_tid
        ids = resolve_names([product_name])
        product_tid = ids.get(product_name)
        if not product_tid:
            raise ValueError(f"找不到物品: {product_name}")

    # 获取蓝图
    bp = get_blueprint(product_tid)

    # 获取成本指数
    cost_index = get_cost_index(system)
    total_mfg_rate = cost_index + BUILDING_TAX + SCC_SURCHARGE

    # 收集所有需要查价的 type_id
    all_tids = set()
    for m in bp["materials"]:
        all_tids.add(m["typeid"])
    if include_invention:
        for m in bp["inv_materials"]:
            all_tids.add(m["typeid"])
    all_tids.add(product_tid)

    # 获取价格
    prices = get_market_prices(list(all_tids))

    me_mult = 1 - 0.01 * me
    price_key = "sell_min" if buy_mode == "sell" else "buy_max"

    # ---- 制造材料成本 ----
    mat_details = []
    total_mat_cost = 0
    for m in bp["materials"]:
        tid = m["typeid"]
        base_qty = m["quantity"]
        total_qty = math.ceil(base_qty * runs * me_mult)
        unit_price = prices.get(tid, {}).get(price_key) or 0
        cost = total_qty * unit_price
        total_mat_cost += cost
        mat_details.append({
            "name": m.get("name", str(tid)),
            "tid": tid,
            "base_qty": base_qty,
            "total_qty": total_qty,
            "unit_price": unit_price,
            "cost": cost,
        })

    # ---- 制造费用 ----
    mfg_fee_per_run = bp["adjustedPrice"] * total_mfg_rate
    total_mfg_fee = mfg_fee_per_run * runs

    # ---- 发明摊销 ----
    inv_details = []
    inv_per_run = 0
    inv_cost_per_attempt = 0
    if include_invention and bp["inv_materials"]:
        for m in bp["inv_materials"]:
            tid = m["typeid"]
            qty = m["quantity"]
            unit_price = prices.get(tid, {}).get(price_key) or 0
            cost = qty * unit_price
            inv_cost_per_attempt += cost
            inv_details.append({
                "name": m.get("name", str(tid)),
                "tid": tid,
                "qty": qty,
                "unit_price": unit_price,
                "cost": cost,
            })
        prob = bp["probability"]
        if prob > 0:
            attempts_needed = 1 / prob
            inv_per_run = (inv_cost_per_attempt * attempts_needed) / runs

    # ---- 汇总 ----
    product_qty = bp.get("productQty", 1)  # 每run产出数量
    total_units = runs * product_qty
    inv_per_unit = inv_per_run / product_qty if product_qty > 0 else inv_per_run
    total_cost_per_unit = (total_mat_cost + total_mfg_fee) / total_units + inv_per_unit
    product_price = prices.get(product_tid, {})
    sell_price = product_price.get("sell_min") or 0
    buy_price = product_price.get("buy_max") or 0
    net_receive = sell_price * (1 - SALES_TAX)
    profit = net_receive - total_cost_per_unit
    margin = (profit / total_cost_per_unit * 100) if total_cost_per_unit > 0 else 0

    return {
        "product_name": product_name,
        "product_tid": product_tid,
        "me": me,
        "runs": runs,
        "system": system,
        "cost_index": cost_index,
        "total_mfg_rate": total_mfg_rate,
        "invention_probability": bp["probability"],
        "adjustedPrice": bp["adjustedPrice"],
        "mfg_time_seconds": bp["mfg_time"],
        "product_qty": product_qty,
        "total_units": total_units,
        "materials": mat_details,
        "mfg_fee_per_run": mfg_fee_per_run,
        "invention": inv_details,
        "inv_cost_per_attempt": inv_cost_per_attempt,
        "inv_per_run": inv_per_run,
        "total_mat_cost": total_mat_cost,
        "total_mfg_fee": total_mfg_fee,
        "total_cost_per_unit": total_cost_per_unit,
        "sell_price": sell_price,
        "buy_price": buy_price,
        "net_receive": net_receive,
        "profit": profit,
        "margin": margin,
    }


# ============================================================
# 格式化输出
# ============================================================
def format_result(r):
    lines = []
    lines.append(f"{'═' * 55}")
    qty_info = f", {r['product_qty']}/run" if r.get('product_qty', 1) > 1 else ""
    lines.append(f"  {r['product_name']}  (ME={r['me']}, {r['runs']} runs{qty_info})")
    if r.get('total_units', 0) > r['runs']:
        lines.append(f"  总产出: {r['total_units']:,} 个")
    lines.append(f"  制造星系: {r['system']} ({r['cost_index']*100:.2f}%)")
    lines.append(f"{'═' * 55}")

    # 材料
    lines.append(f"\n📦 制造材料:")
    for m in r["materials"]:
        lines.append(f"  {m['name']}: {m['total_qty']} × {m['unit_price']:,.0f} = {m['cost']:,.0f}")
    lines.append(f"  材料合计: {r['total_mat_cost']:,.0f}")

    # 制造费
    lines.append(f"\n⚙️  制造费用: {r['adjustedPrice']:,.0f} × {r['total_mfg_rate']*100:.2f}% = {r['mfg_fee_per_run']:,.0f}/run")
    lines.append(f"  制造费合计: {r['total_mfg_fee']:,.0f}")

    # 发明
    if r["invention"]:
        lines.append(f"\n🔬 发明 (成功率 {r['invention_probability']*100:.0f}%):")
        for m in r["invention"]:
            lines.append(f"  {m['name']}: {m['qty']} × {m['unit_price']:,.0f} = {m['cost']:,.0f}")
        lines.append(f"  单次尝试: {r['inv_cost_per_attempt']:,.0f}")
        lines.append(f"  每run摊销: {r['inv_per_run']:,.0f}")

    # 汇总
    lines.append(f"\n{'─' * 55}")
    lines.append(f"  💰 每只总成本: {r['total_cost_per_unit']:,.0f}")
    lines.append(f"  📈 卖价: {r['sell_price']:,.0f}")
    lines.append(f"  📉 买单收: {r['buy_price']:,.0f}")
    lines.append(f"  💵 实收(扣4%税): {r['net_receive']:,.0f}")

    profit_emoji = "✅" if r["profit"] >= 0 else "❌"
    lines.append(f"\n  {profit_emoji} 每只净利润: {r['profit']:+,.0f}")
    lines.append(f"  📊 利润率: {r['margin']:+.1f}%")

    # 总利润
    total_units = r.get('total_units', r['runs'])
    total_profit = r['profit'] * total_units
    if total_units > r['runs']:
        lines.append(f"  📦 {r['runs']}run × {r['product_qty']:,}/run = {total_units:,} 个")
        lines.append(f"  📦 总利润: {total_profit:+,.0f}")
    else:
        lines.append(f"  📦 {r['runs']}run 总利润: {total_profit:+,.0f}")
    lines.append(f"{'═' * 55}")

    return "\n".join(lines)


# ============================================================
# CLI
# ============================================================
def main():
    parser = argparse.ArgumentParser(description="EVE T2 制造利润计算器")
    parser.add_argument("items", nargs="+", help="产品名称或type_id")
    parser.add_argument("--me", type=int, default=DEFAULT_ME, help=f"材料效率 (默认{DEFAULT_ME})")
    parser.add_argument("--runs", type=int, default=DEFAULT_RUNS, help=f"制造次数 (默认{DEFAULT_RUNS})")
    parser.add_argument("--system", default="haajinen", help="制造星系 (默认haajinen)")
    parser.add_argument("--no-invention", action="store_true", help="不计算发明摊销")
    parser.add_argument("--buy-mode", choices=["sell", "buy"], default="sell",
                        help="材料价格: sell=卖价买入, buy=买单收 (默认sell)")
    parser.add_argument("--json", action="store_true", help="JSON输出")

    args = parser.parse_args()

    results = []
    for item in args.items:
        try:
            r = calculate(
                item, me=args.me, runs=args.runs,
                system=args.system,
                include_invention=not args.no_invention,
                buy_mode=args.buy_mode,
            )
            results.append(r)
        except Exception as e:
            print(f"❌ {item}: {e}", file=sys.stderr)

    if args.json:
        print(json.dumps(results, indent=2, ensure_ascii=False))
    else:
        for r in results:
            print(format_result(r))


if __name__ == "__main__":
    main()
