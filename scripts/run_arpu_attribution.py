#!/usr/bin/env python3
"""Run C-end ARPU attribution queries via c-query-cli-lite's SQLExecutor."""

from __future__ import annotations

import argparse
import calendar
import csv
import importlib.util
import json
import os
from pathlib import Path
from time import monotonic
from typing import Any


def month_bounds(month_id: int) -> tuple[int, int]:
    year = month_id // 100
    month = month_id % 100
    last_day = calendar.monthrange(year, month)[1]
    return int(f"{year}{month:02d}01"), int(f"{year}{month:02d}{last_day:02d}")


def previous_month(month_id: int) -> int:
    year = month_id // 100
    month = month_id % 100
    if month == 1:
        return (year - 1) * 100 + 12
    return year * 100 + month - 1


def resolve_query_cli_root(value: str | None) -> Path:
    if value:
        root = Path(value).expanduser().resolve()
    elif os.getenv("C_QUERY_CLI_LITE_ROOT"):
        root = Path(os.environ["C_QUERY_CLI_LITE_ROOT"]).expanduser().resolve()
    else:
        root = Path(__file__).resolve().parents[2].parent / "c-query-cli-lite"

    if not (root / "src" / "executor.py").is_file():
        raise SystemExit(f"找不到 c-query-cli-lite 执行器: {root / 'src' / 'executor.py'}")
    return root


def load_query_cli_executor(query_cli_root: Path):
    executor_path = query_cli_root / "src" / "executor.py"
    spec = importlib.util.spec_from_file_location("c_query_cli_lite_executor", executor_path)
    if spec is None or spec.loader is None:
        raise SystemExit(f"无法加载执行器: {executor_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.SQLExecutor


def load_query_cli_config(query_cli_root: Path, config_path: str | None) -> dict[str, Any]:
    path = Path(config_path).expanduser().resolve() if config_path else query_cli_root / "config.json"
    if not path.is_file():
        raise SystemExit(
            f"配置文件不存在: {path}\n"
            "请先按 c-query-cli-lite 的方式复制 config.example.json 为 config.json，并填入数据库账号密码。"
        )
    return json.loads(path.read_text(encoding="utf-8"))


def sql_base(start_day: int, end_day: int) -> str:
    return f"""
with active_pool as (
  select distinct
    cast(substr(cast(day as string), 1, 6) as int) as month_id,
    u_user,
    coalesce(business_user_pay_status_statistics_month, '未知') as user_status
  from aws.business_active_user_last_14_day
  where day between {start_day} and {end_day}
    and u_user is not null
),
active_total as (
  select month_id, count(distinct u_user) as active_users
  from active_pool
  group by month_id
),
orders as (
  select
    cast(substr(cast(o.paid_time_sk as string), 1, 6) as int) as month_id,
    a.user_status,
    coalesce(o.business_gmv_attribution, '未知') as gmv_channel,
    concat(
      coalesce(o.business_good_kind_name_level_2, '未知'),
      ' / ',
      coalesce(o.business_good_kind_name_level_3, '未知')
    ) as product_l3,
    coalesce(o.good_name, '未知') as good_name,
    o.u_user,
    o.order_id,
    o.sub_amount
  from dws.topic_order_detail o
  join active_pool a
    on o.u_user = a.u_user
   and cast(substr(cast(o.paid_time_sk as string), 1, 6) as int) = a.month_id
  where o.is_test_user = 0
    and o.paid_time_sk between {start_day} and {end_day}
    and o.u_user is not null
    and o.original_amount >= 39
    and o.business_gmv_attribution in ('电销', '商业化')
)
"""


def core_sql(start_day: int, end_day: int) -> str:
    return sql_base(start_day, end_day) + """
, order_month as (
  select
    month_id,
    count(distinct u_user) as pay_users,
    count(distinct order_id) as order_cnt,
    round(sum(sub_amount), 2) as revenue
  from orders
  group by month_id
)
select
  a.month_id,
  a.active_users,
  coalesce(o.pay_users, 0) as pay_users,
  coalesce(o.order_cnt, 0) as order_cnt,
  coalesce(o.revenue, 0) as revenue,
  round(coalesce(o.revenue, 0) / nullif(a.active_users, 0), 4) as arpu,
  round(coalesce(o.pay_users, 0) / nullif(a.active_users, 0), 8) as pay_rate,
  round(coalesce(o.revenue, 0) / nullif(o.pay_users, 0), 4) as arppu,
  round(coalesce(o.revenue, 0) / nullif(o.order_cnt, 0), 4) as aov
from active_total a
left join order_month o on a.month_id = o.month_id
order by a.month_id
"""


def dimension_sql(
    start_day: int,
    end_day: int,
    compare_month: int,
    analysis_month: int,
    dimension_expr: str,
    min_revenue: int = 10000,
    limit: int = 80,
) -> str:
    return sql_base(start_day, end_day) + f"""
, agg as (
  select
    {dimension_expr} as dim_name,
    month_id,
    count(distinct u_user) as pay_users,
    count(distinct order_id) as order_cnt,
    round(sum(sub_amount), 2) as revenue
  from orders
  group by {dimension_expr}, month_id
),
wide as (
  select
    dim_name,
    max(case when month_id = {compare_month} then pay_users else 0 end) as pay_users_base,
    max(case when month_id = {analysis_month} then pay_users else 0 end) as pay_users_current,
    max(case when month_id = {compare_month} then order_cnt else 0 end) as order_cnt_base,
    max(case when month_id = {analysis_month} then order_cnt else 0 end) as order_cnt_current,
    max(case when month_id = {compare_month} then revenue else 0 end) as revenue_base,
    max(case when month_id = {analysis_month} then revenue else 0 end) as revenue_current
  from agg
  group by dim_name
),
totals as (
  select
    max(case when month_id = {compare_month} then active_users else 0 end) as active_base,
    max(case when month_id = {analysis_month} then active_users else 0 end) as active_current
  from active_total
)
select
  w.dim_name,
  w.pay_users_base,
  w.pay_users_current,
  w.pay_users_current - w.pay_users_base as pay_users_delta,
  w.order_cnt_base,
  w.order_cnt_current,
  w.revenue_base,
  w.revenue_current,
  round(w.revenue_current - w.revenue_base, 2) as revenue_delta,
  round(w.revenue_base / nullif(w.pay_users_base, 0), 2) as arppu_base,
  round(w.revenue_current / nullif(w.pay_users_current, 0), 2) as arppu_current,
  round(w.revenue_base / nullif(t.active_base, 0), 4) as arpu_contrib_base,
  round(w.revenue_current / nullif(t.active_current, 0), 4) as arpu_contrib_current,
  round(
    w.revenue_current / nullif(t.active_current, 0)
    - w.revenue_base / nullif(t.active_base, 0),
    4
  ) as arpu_contrib_delta
from wide w
join totals t on 1 = 1
where w.revenue_base >= {min_revenue} or w.revenue_current >= {min_revenue}
order by arpu_contrib_delta asc
limit {limit}
"""


def strategy_sql(start_day: int, end_day: int, compare_month: int, analysis_month: int) -> str:
    return sql_base(start_day, end_day) + f"""
, strategy_orders as (
  select
    month_id,
    good_name,
    count(distinct u_user) as pay_users,
    count(distinct order_id) as order_cnt,
    round(sum(sub_amount), 2) as revenue
  from orders
  where good_name like '%规划提分课%' or good_name like '%全面进阶课%'
  group by month_id, good_name
),
wide as (
  select
    good_name,
    max(case when month_id = {compare_month} then pay_users else 0 end) as pay_users_base,
    max(case when month_id = {analysis_month} then pay_users else 0 end) as pay_users_current,
    max(case when month_id = {compare_month} then order_cnt else 0 end) as order_cnt_base,
    max(case when month_id = {analysis_month} then order_cnt else 0 end) as order_cnt_current,
    max(case when month_id = {compare_month} then revenue else 0 end) as revenue_base,
    max(case when month_id = {analysis_month} then revenue else 0 end) as revenue_current
  from strategy_orders
  group by good_name
)
select
  good_name,
  pay_users_base,
  pay_users_current,
  pay_users_current - pay_users_base as pay_users_delta,
  order_cnt_base,
  order_cnt_current,
  revenue_base,
  revenue_current,
  round(revenue_current - revenue_base, 2) as revenue_delta
from wide
where revenue_base >= 50000 or revenue_current >= 50000
order by revenue_delta asc
"""


def build_queries(analysis_month: int, compare_month: int) -> dict[str, str]:
    compare_start, _ = month_bounds(compare_month)
    _, analysis_end = month_bounds(analysis_month)
    return {
        "core_monthly": core_sql(compare_start, analysis_end),
        "user_status": dimension_sql(compare_start, analysis_end, compare_month, analysis_month, "user_status", limit=40),
        "gmv_channel": dimension_sql(compare_start, analysis_end, compare_month, analysis_month, "gmv_channel", limit=20),
        "product_l3": dimension_sql(compare_start, analysis_end, compare_month, analysis_month, "product_l3", limit=80),
        "good_name": dimension_sql(
            compare_start,
            analysis_end,
            compare_month,
            analysis_month,
            "good_name",
            min_revenue=50000,
            limit=120,
        ),
        "strategy_goods": strategy_sql(compare_start, analysis_end, compare_month, analysis_month),
    }


def run_query(executor: Any, sql: str, engine: str) -> tuple[Any, str, float]:
    start = monotonic()
    if engine == "spark":
        df = executor._execute_sparksql("-- Engine: Spark\n" + sql)
        return df, "SparkSQL", monotonic() - start
    if engine == "starrocks":
        df = executor._execute_starrocks(sql)
        return df, "StarRocks", monotonic() - start
    df, actual_engine, elapsed = executor.execute(sql)
    return df, actual_engine, elapsed


def write_query_sql(path: Path, sql: str) -> None:
    path.write_text(sql.strip() + "\n", encoding="utf-8")


def validate_csv(path: Path) -> int:
    with path.open(newline="", encoding="utf-8") as csv_file:
        return max(sum(1 for _ in csv.reader(csv_file)) - 1, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run C-end ARPU attribution queries.")
    parser.add_argument("--analysis-month", required=True, type=int, help="分析月份，例如 202604")
    parser.add_argument("--compare-month", type=int, help="对比月份，例如 202603；不传则默认分析月上月")
    parser.add_argument("--output-dir", type=Path, default=None, help="CSV 输出目录")
    parser.add_argument("--query-cli-root", default=None, help="c-query-cli-lite 项目路径；默认使用同级目录")
    parser.add_argument("--config", default=None, help="c-query-cli-lite config.json 路径；默认读取项目根目录 config.json")
    parser.add_argument("--engine", choices=["auto", "starrocks", "spark"], default="auto", help="执行引擎")
    args = parser.parse_args()

    analysis_month = args.analysis_month
    compare_month = args.compare_month or previous_month(analysis_month)
    output_dir = args.output_dir or Path(f"outputs/c_end_arpu_{analysis_month}_vs_{compare_month}")
    output_dir.mkdir(parents=True, exist_ok=True)

    query_cli_root = resolve_query_cli_root(args.query_cli_root)
    SQLExecutor = load_query_cli_executor(query_cli_root)
    config = load_query_cli_config(query_cli_root, args.config)
    executor = SQLExecutor(config)

    metadata: list[dict[str, Any]] = []
    for name, sql in build_queries(analysis_month, compare_month).items():
        start = monotonic()
        df, actual_engine, elapsed = run_query(executor, sql, args.engine)
        csv_path = output_dir / f"{name}.csv"
        sql_path = output_dir / f"{name}.sql"
        df.to_csv(csv_path, index=False, encoding="utf-8")
        write_query_sql(sql_path, sql)
        row_count = validate_csv(csv_path)
        metadata.append(
            {
                "name": name,
                "engine": actual_engine,
                "rows": row_count,
                "elapsed_seconds": round(elapsed, 2),
                "wall_seconds": round(monotonic() - start, 2),
                "csv": str(csv_path),
                "sql": str(sql_path),
            }
        )
        print(f"{name}: {row_count} rows via {actual_engine} -> {csv_path} ({elapsed:.1f}s)")

    (output_dir / "metadata.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Done. Output directory: {output_dir}")


if __name__ == "__main__":
    main()
