"""业务库建模与造数（M4）。

用法（在 backend 目录下执行）：
    ..\\.venv\\Scripts\\python.exe scripts\\seed_biz_db.py                # 建 schema + 建表 + 造数（幂等）
    ..\\.venv\\Scripts\\python.exe scripts\\seed_biz_db.py --bind-user 48 --bind-employee E1001
        # 把某个员工绑定到登录用户，用于验证 member 只能查本人考勤/薪资

数据形态（决策 A：同一 PostgreSQL 实例两个 schema）：
- biz_a：customer(20) / orders(3000，跨 120 天，其中前 300 单为基线分布) / product(15) / inventory(30)
- biz_b：employee(20) / attendance(≥500，近 40 天工作日) / payroll(60，20 人 × 3 个月)
所有数据均为演示用虚构数据，手机号/身份证符合格式但不对应真实主体。
"""
from __future__ import annotations

import argparse
import random
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND_DIR.parent))

import psycopg  # noqa: E402

from backend.config import settings  # noqa: E402

# 订单总量：3000 单，用于同时覆盖「小结果内联」与「大结果 3000 行导出」（需求 4.6）
ORDER_TOTAL = 3000

SCHEMA_SQL = """
CREATE SCHEMA IF NOT EXISTS biz_a;
CREATE SCHEMA IF NOT EXISTS biz_b;

CREATE TABLE IF NOT EXISTS biz_a.customer (
    id            BIGSERIAL PRIMARY KEY,
    name          VARCHAR(32) NOT NULL,
    phone         VARCHAR(20) NOT NULL,
    id_card       VARCHAR(24) NOT NULL,
    email         VARCHAR(64),
    city          VARCHAR(32),
    user_id       BIGINT,
    register_date DATE NOT NULL
);

-- 跨 schema 关联：客户归属员工（biz_a.customer -> biz_b.employee），用于演示跨库联表
ALTER TABLE biz_a.customer ADD COLUMN IF NOT EXISTS owner_employee_id BIGINT;

CREATE TABLE IF NOT EXISTS biz_a.product (
    id       BIGSERIAL PRIMARY KEY,
    sku      VARCHAR(20) NOT NULL UNIQUE,
    name     VARCHAR(64) NOT NULL,
    category VARCHAR(32) NOT NULL,
    price    NUMERIC(12,2) NOT NULL
);

CREATE TABLE IF NOT EXISTS biz_a.orders (
    id          BIGSERIAL PRIMARY KEY,
    order_no    VARCHAR(32) NOT NULL UNIQUE,
    customer_id BIGINT NOT NULL REFERENCES biz_a.customer(id),
    product_id  BIGINT NOT NULL REFERENCES biz_a.product(id),
    qty         INT NOT NULL,
    amount      NUMERIC(12,2) NOT NULL,
    status      VARCHAR(16) NOT NULL,
    created_at  TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS biz_a.inventory (
    id         BIGSERIAL PRIMARY KEY,
    sku        VARCHAR(20) NOT NULL,
    product_id BIGINT NOT NULL REFERENCES biz_a.product(id),
    warehouse  VARCHAR(32) NOT NULL,
    qty        INT NOT NULL,
    updated_at TIMESTAMP NOT NULL
);

CREATE TABLE IF NOT EXISTS biz_b.employee (
    id          BIGSERIAL PRIMARY KEY,
    employee_no VARCHAR(20) NOT NULL UNIQUE,
    name        VARCHAR(32) NOT NULL,
    phone       VARCHAR(20) NOT NULL,
    dept        VARCHAR(32) NOT NULL,
    position    VARCHAR(32) NOT NULL,
    hire_date   DATE NOT NULL,
    user_id     BIGINT
);

CREATE TABLE IF NOT EXISTS biz_b.attendance (
    id          BIGSERIAL PRIMARY KEY,
    employee_id BIGINT NOT NULL REFERENCES biz_b.employee(id),
    work_date   DATE NOT NULL,
    check_in    VARCHAR(8),
    check_out   VARCHAR(8),
    status      VARCHAR(16) NOT NULL
);

CREATE TABLE IF NOT EXISTS biz_b.payroll (
    id          BIGSERIAL PRIMARY KEY,
    employee_id BIGINT NOT NULL REFERENCES biz_b.employee(id),
    period      VARCHAR(7) NOT NULL,
    base_salary NUMERIC(12,2) NOT NULL,
    bonus       NUMERIC(12,2) NOT NULL,
    deduction   NUMERIC(12,2) NOT NULL,
    net_salary  NUMERIC(12,2) NOT NULL
);
"""

CUSTOMER_NAMES = [
    "张三", "李四", "王五", "张伟", "李娜", "王芳", "刘洋", "陈静", "赵敏", "周杰",
    "吴磊", "徐丽", "孙涛", "马超", "朱婷", "胡军", "郭敬", "何静", "高原", "林峰",
]
CITIES = ["广州", "深圳", "上海", "北京", "杭州", "成都", "武汉", "南京"]
EMPLOYEE_NAMES = [
    "杨帆", "黄晓", "许诺", "曹阳", "沈月", "韩磊", "冯雷", "邓超", "程琳", "蔡明",
    "潘婷", "袁泉", "于洋", "董健", "余静", "苏晴", "卢伟", "蒋华", "杜鹏", "范雨",
]
DEPTS = ["销售部", "技术部", "客服部", "财务部", "仓储部"]
POSITIONS = ["专员", "主管", "经理", "工程师", "助理"]
CATEGORIES = ["家用电器", "数码配件", "办公用品", "清洁用品", "食品"]
WAREHOUSES = ["广州仓", "上海仓"]
PERIODS = ["2026-09", "2026-10", "2026-11"]


def _phone(i: int) -> str:
    return f"1{random.choice(['38', '39', '50', '86', '88'])}{i:04d}{random.randint(1000, 9999)}"


def _id_card(i: int) -> str:
    return f"4401{i % 100:02d}{1990 + i % 20}{(i % 12) + 1:02d}{(i % 28) + 1:02d}1234"


def seed(dsn: str, bind_user: int | None = None, bind_employee: str = "E1001") -> dict:
    random.seed(20261111)
    today = date.today()
    with psycopg.connect(dsn, autocommit=False) as conn:
        with conn.cursor() as cur:
            for stmt in filter(None, (s.strip() for s in SCHEMA_SQL.split(";"))):
                cur.execute(stmt)

            # 先清空演示数据，保证脚本可重复执行；表结构保留
            cur.execute("TRUNCATE biz_a.orders, biz_a.inventory, biz_a.orders, biz_a.customer, biz_a.product, biz_b.attendance, biz_b.payroll, biz_b.employee RESTART IDENTITY CASCADE")

            customers = []
            for i, name in enumerate(CUSTOMER_NAMES, start=1):
                register = today - timedelta(days=random.randint(60, 900))
                cur.execute(
                    "INSERT INTO biz_a.customer (name, phone, id_card, email, city, user_id, register_date) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s) RETURNING id",
                    (
                        name, _phone(i), _id_card(i), f"user{i:02d}@example.com",
                        random.choice(CITIES), (1 if name == "张三" else None), register,
                    ),
                )
                customers.append((cur.fetchone()[0], name))

            products = []
            for i in range(1, 16):
                sku = f"P{i:03d}"
                name = f"演示商品{i:02d}"
                price = round(random.uniform(29, 1299), 2)
                cur.execute(
                    "INSERT INTO biz_a.product (sku, name, category, price) VALUES (%s, %s, %s, %s) RETURNING id",
                    (sku, name, random.choice(CATEGORIES), price),
                )
                products.append((cur.fetchone()[0], sku, price))

            statuses = ["paid", "shipped", "closed", "refunded"]
            for i in range(300):
                customer_id, _ = customers[i % len(customers)]
                product_id, _, price = products[i % len(products)]
                qty = random.randint(1, 5)
                # 造数落在 0~118 天：留出边界余量，避免"近120天"查询在当天就漏掉边界单
                created = datetime.now() - timedelta(days=random.randint(0, 118), hours=random.randint(0, 23))
                cur.execute(
                    "INSERT INTO biz_a.orders (order_no, customer_id, product_id, qty, amount, status, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        f"SO{today.strftime('%Y%m')}{i:05d}", customer_id, product_id, qty,
                        round(price * qty, 2), statuses[i % len(statuses)], created,
                    ),
                )

            # 扩量到 3000 单（需求 4.6 的「3000 行结果」用例）：
            # 前 300 单保持原有分布不动，便于 M4 的「张三 11 单 / 杨帆名下客户 11 单」断言继续成立；
            # 后 2700 单只发给「非张三、且不归杨帆管」的客户，避免改变这两条既有用例的行数。
            yangfan_emp_index = EMPLOYEE_NAMES.index("杨帆") if "杨帆" in EMPLOYEE_NAMES else None
            protected = {customer_id for customer_id, name in customers if name == "张三"}
            if yangfan_emp_index is not None:
                # 客户 index % 员工数 决定归属（与下面 owner 回填同一规则）
                for index, (customer_id, _) in enumerate(customers):
                    if index % len(EMPLOYEE_NAMES) == yangfan_emp_index:
                        protected.add(customer_id)
            bulk_customers = [cid for cid, _ in customers if cid not in protected]
            for i in range(300, ORDER_TOTAL):
                customer_id = bulk_customers[i % len(bulk_customers)]
                product_id, _, price = products[i % len(products)]
                qty = random.randint(1, 5)
                # 造数落在 0~118 天：留出边界余量，避免"近120天"查询在当天就漏掉边界单
                created = datetime.now() - timedelta(days=random.randint(0, 118), hours=random.randint(0, 23))
                cur.execute(
                    "INSERT INTO biz_a.orders (order_no, customer_id, product_id, qty, amount, status, created_at) "
                    "VALUES (%s, %s, %s, %s, %s, %s, %s)",
                    (
                        f"SO{today.strftime('%Y%m')}{i:05d}", customer_id, product_id, qty,
                        round(price * qty, 2), statuses[i % len(statuses)], created,
                    ),
                )

            for product_id, sku, _ in products:
                for warehouse in WAREHOUSES:
                    cur.execute(
                        "INSERT INTO biz_a.inventory (sku, product_id, warehouse, qty, updated_at) "
                        "VALUES (%s, %s, %s, %s, now() - interval '%s hours')",
                        (sku, product_id, warehouse, random.randint(0, 500), random.randint(1, 240)),
                    )

            employees = []
            for i, name in enumerate(EMPLOYEE_NAMES, start=1):
                employee_no = f"E{1000 + i}"
                hire = today - timedelta(days=random.randint(200, 2000))
                cur.execute(
                    "INSERT INTO biz_b.employee (employee_no, name, phone, dept, position, hire_date, user_id) "
                    "VALUES (%s, %s, %s, %s, %s, %s, NULL) RETURNING id",
                    (employee_no, name, _phone(100 + i), random.choice(DEPTS),
                     random.choice(POSITIONS), hire),
                )
                employees.append((cur.fetchone()[0], employee_no))

            # 跨 schema 关联：20 个客户按顺序轮流归属 20 个员工（biz_a.customer.owner_employee_id -> biz_b.employee.id）
            for index, (customer_id, _) in enumerate(customers):
                employee_id, _ = employees[index % len(employees)]
                cur.execute(
                    "UPDATE biz_a.customer SET owner_employee_id = %s WHERE id = %s",
                    (employee_id, customer_id),
                )

            att_statuses = ["normal", "normal", "normal", "late", "leave"]
            for employee_id, _ in employees:
                for offset in range(40):
                    work = today - timedelta(days=offset)
                    if work.weekday() >= 5:
                        continue
                    status = att_statuses[(offset + employee_id) % len(att_statuses)]
                    check_in = "09:0%d" % ((offset + employee_id) % 9) if status != "leave" else None
                    check_out = "18:3%d" % ((offset * 2 + employee_id) % 9) if status != "leave" else None
                    if status == "late":
                        check_in = "09:4%d" % ((offset + employee_id) % 9)
                    cur.execute(
                        "INSERT INTO biz_b.attendance (employee_id, work_date, check_in, check_out, status) "
                        "VALUES (%s, %s, %s, %s, %s)",
                        (employee_id, work, check_in, check_out, status),
                    )

            for employee_id, _ in employees:
                base = float(random.choice([7000, 9000, 12000, 15000, 20000]))
                for period in PERIODS:
                    bonus = round(random.uniform(0, 3000), 2)
                    deduction = round(random.uniform(0, 800), 2)
                    cur.execute(
                        "INSERT INTO biz_b.payroll (employee_id, period, base_salary, bonus, deduction, net_salary) "
                        "VALUES (%s, %s, %s, %s, %s, %s)",
                        (employee_id, period, base, bonus, deduction, round(base + bonus - deduction, 2)),
                    )

            if bind_user is not None:
                cur.execute("UPDATE biz_b.employee SET user_id = %s WHERE employee_no = %s", (bind_user, bind_employee))
                cur.execute("UPDATE biz_a.customer SET user_id = %s WHERE name = '张三'", (bind_user,))

        conn.commit()
        with conn.cursor() as cur:
            counts = {}
            for schema, table in (
                ("biz_a", "customer"), ("biz_a", "product"), ("biz_a", "orders"), ("biz_a", "inventory"),
                ("biz_b", "employee"), ("biz_b", "attendance"), ("biz_b", "payroll"),
            ):
                cur.execute(f"SELECT count(*) FROM {schema}.{table}")
                counts[f"{schema}.{table}"] = cur.fetchone()[0]
    return counts


def main() -> None:
    parser = argparse.ArgumentParser(description="建 biz_a/biz_b schema 并造演示数据")
    parser.add_argument("--dsn", default=settings.DB_DSN, help="目标库 DSN（默认取 .env 的 DB_DSN）")
    parser.add_argument("--bind-user", type=int, default=None, help="把某个员工绑到该登录用户（验证 member_self）")
    parser.add_argument("--bind-employee", default="E1001", help="配合 --bind-user 使用的员工工号")
    args = parser.parse_args()
    counts = seed(args.dsn, bind_user=args.bind_user, bind_employee=args.bind_employee)
    for key, value in counts.items():
        print(f"{key}: {value}")


if __name__ == "__main__":
    main()
