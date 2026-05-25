#!/usr/bin/env python3
"""Run C-end ARPU attribution queries and export CSV results."""

from __future__ import annotations

import argparse
import calendar
import csv
import os
from pathlib import Path
from textwrap import dedent
from time import monotonic

import paramiko


REQUIRED_ENV = [
    "HIGH_VALUE_SSH_HOST",
    "HIGH_VALUE_SSH_USER",
    "HIGH_VALUE_SSH_PASS",
    "HIGH_VALUE_DB_HOST",
    "HIGH_VALUE_DB_PORT",
    "HIGH_VALUE_DB_USER",
    "HIGH_VALUE_DB_PASS",
]


def require_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


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
        "user_status": dimension_sql(
            compare_start,
            analysis_end,
            compare_month,
            analysis_month,
            "user_status",
            min_revenue=10000,
            limit=40,
        ),
        "gmv_channel": dimension_sql(
            compare_start,
            analysis_end,
            compare_month,
            analysis_month,
            "gmv_channel",
            min_revenue=10000,
            limit=20,
        ),
        "product_l3": dimension_sql(
            compare_start,
            analysis_end,
            compare_month,
            analysis_month,
            "product_l3",
            min_revenue=10000,
            limit=80,
        ),
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


def remote_script(sql: str, db: dict[str, str]) -> str:
    return dedent(
        f"""
        import csv
        import sys
        import warnings
        from impala.dbapi import connect

        warnings.filterwarnings("ignore")
        conn = connect(
            host={db["host"]!r},
            port={int(db["port"])},
            user={db["user"]!r},
            password={db["password"]!r},
            auth_mechanism="PLAIN",
        )
        cur = conn.cursor()
        cur.execute({sql!r})
        writer = csv.writer(sys.stdout)
        writer.writerow([d[0] for d in cur.description])
        writer.writerows(cur.fetchall())
        cur.close()
        conn.close()
        """
    )


def run_query(ssh: paramiko.SSHClient, sql: str, db: dict[str, str], remote_name: str) -> str:
    script = remote_script(sql, db)
    remote_path = f"/tmp/{remote_name}.py"
    with ssh.open_sftp() as sftp:
        with sftp.file(remote_path, "w") as remote_file:
            remote_file.write(script)

    _, stdout, stderr = ssh.exec_command(f"python3 {remote_path}", timeout=900)
    exit_code = stdout.channel.recv_exit_status()
    output = stdout.read().decode("utf-8", errors="replace")
    error = stderr.read().decode("utf-8", errors="replace")
    if exit_code != 0:
        raise RuntimeError(f"{remote_name} failed with exit={exit_code}: {error[-3000:]}")
    return output


def validate_csv(text: str) -> int:
    rows = list(csv.reader(text.splitlines()))
    return max(len(rows) - 1, 0)


def main() -> None:
    parser = argparse.ArgumentParser(description="Run C-end ARPU attribution queries.")
    parser.add_argument("--analysis-month", required=True, type=int, help="分析月份，例如 202604")
    parser.add_argument("--compare-month", type=int, help="对比月份，例如 202603；不传则默认分析月上月")
    parser.add_argument("--output-dir", type=Path, default=None, help="CSV 输出目录")
    args = parser.parse_args()

    missing = [name for name in REQUIRED_ENV if not os.getenv(name)]
    if missing:
        raise SystemExit("Missing required environment variables: " + ", ".join(missing))

    analysis_month = args.analysis_month
    compare_month = args.compare_month or previous_month(analysis_month)
    output_dir = args.output_dir or Path(f"outputs/c_end_arpu_{analysis_month}_vs_{compare_month}")
    output_dir.mkdir(parents=True, exist_ok=True)

    ssh_conf = {
        "host": require_env("HIGH_VALUE_SSH_HOST"),
        "user": require_env("HIGH_VALUE_SSH_USER"),
        "password": require_env("HIGH_VALUE_SSH_PASS"),
    }
    db_conf = {
        "host": require_env("HIGH_VALUE_DB_HOST"),
        "port": require_env("HIGH_VALUE_DB_PORT"),
        "user": require_env("HIGH_VALUE_DB_USER"),
        "password": require_env("HIGH_VALUE_DB_PASS"),
    }

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(
        ssh_conf["host"],
        username=ssh_conf["user"],
        password=ssh_conf["password"],
        timeout=30,
        banner_timeout=30,
        auth_timeout=30,
    )
    try:
        for name, sql in build_queries(analysis_month, compare_month).items():
            start = monotonic()
            result = run_query(ssh, sql, db_conf, f"c_end_arpu_{analysis_month}_{name}")
            row_count = validate_csv(result)
            path = output_dir / f"{name}.csv"
            path.write_text(result, encoding="utf-8")
            elapsed = monotonic() - start
            print(f"{name}: {row_count} rows -> {path} ({elapsed:.1f}s)")
    finally:
        ssh.close()

    print(f"Done. Output directory: {output_dir}")


if __name__ == "__main__":
    main()
