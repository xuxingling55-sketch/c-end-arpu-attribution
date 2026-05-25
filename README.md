# C 端 ARPU 归因分析项目

这个项目沉淀 C 端 ARPU / 付费率 / 营收 / 客单价变化的“取数 + 写作”归因工作流。

核心目标：

- 当用户询问“为什么某月 C 端 ARPU 变了”时，先确认分析月份；未给时间必须反问。
- 按统一口径自动取数：C 端活跃池、活跃池内正价营收、付费率、ARPPU、渠道、客群、商品与上新承接。
- 输出带描述性分析的 Markdown 归因文档，而不是只堆表格。

## 项目结构

```text
.
├── .cursor/skills/c-end-arpu-attribution/SKILL.md
├── scripts/run_arpu_attribution.py
├── examples/4月C端ARPU环比下滑归因分析.md
├── outputs/.gitkeep
├── requirements.txt
└── README.md
```

## 快速使用

安装依赖：

```bash
pip install -r requirements.txt
```

设置数据库与 SSH 连接环境变量：

```bash
export HIGH_VALUE_SSH_HOST=...
export HIGH_VALUE_SSH_USER=...
export HIGH_VALUE_SSH_PASS=...
export HIGH_VALUE_DB_HOST=...
export HIGH_VALUE_DB_PORT=...
export HIGH_VALUE_DB_USER=...
export HIGH_VALUE_DB_PASS=...
```

运行 2026 年 4 月对比 3 月的归因取数：

```bash
python3 scripts/run_arpu_attribution.py \
  --analysis-month 202604 \
  --compare-month 202603 \
  --output-dir outputs/c_end_arpu_202604_vs_202603
```

脚本会输出：

- `core_monthly.csv`：整体月指标
- `user_status.csv`：用户客群归因
- `gmv_channel.csv`：商业化 / 电销归因
- `product_l3.csv`：商品类目归因
- `good_name.csv`：单品下钻
- `strategy_goods.csv`：上新 / 切品承接校正

## 口径说明

- 活跃主表：`aws.business_active_user_last_14_day`
- 订单表：`dws.topic_order_detail`
- 营收金额：`sum(sub_amount)`
- 正价筛选：`original_amount >= 39`
- 业务归因：`business_gmv_attribution in ('电销','商业化')`
- 不默认限制 `status = '支付成功'`，只有用户明确要求时才加订单状态条件。

ARPU 分子必须限定在同月 C 端活跃用户池内，不能用全量订单营收直接除以 C 端活跃用户。
