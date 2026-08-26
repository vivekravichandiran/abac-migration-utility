"""Deterministic dummy-data generator for the sales ABAC demo tables.

No third-party faker lib available in this environment, so we hand-roll
small pools of realistic-looking (but fake) Indian names/cities/etc. and
combine them deterministically per catalog so re-runs are idempotent.
"""
from __future__ import annotations

import hashlib
import random

FIRST_NAMES = [
    "Aarav", "Vivaan", "Aditya", "Vihaan", "Arjun", "Sai", "Reyansh", "Krishna",
    "Ishaan", "Rohan", "Ananya", "Diya", "Isha", "Kavya", "Meera", "Priya",
    "Riya", "Saanvi", "Tara", "Zoya",
]
LAST_NAMES = [
    "Sharma", "Verma", "Iyer", "Nair", "Reddy", "Patel", "Gupta", "Menon",
    "Rao", "Bose", "Chatterjee", "Kapoor", "Malhotra", "Bhatia", "Joshi",
]
CITIES_STATES = [
    ("Mumbai", "Maharashtra"), ("Navi Mumbai", "Maharashtra"), ("Pune", "Maharashtra"),
    ("Surat", "Gujarat"), ("Ahmedabad", "Gujarat"), ("Jamnagar", "Gujarat"),
    ("Delhi", "Delhi"), ("Gurugram", "Haryana"), ("Bengaluru", "Karnataka"),
    ("Hyderabad", "Telangana"), ("Chennai", "Tamil Nadu"), ("Kolkata", "West Bengal"),
]
RETAIL_PRODUCTS = [
    ("Reliance Trends T-Shirt", "Apparel"), ("Reliance Digital Smart TV 43in", "Electronics"),
    ("Reliance Fresh Grocery Kit", "Grocery"), ("AJIO Running Shoes", "Footwear"),
    ("Reliance Retail Home Mixer", "Appliances"), ("Reliance Jewels Gold Coin 1g", "Jewellery"),
    ("Reliance Digital Bluetooth Speaker", "Electronics"), ("Reliance Trends Denim Jacket", "Apparel"),
]
O2C_PRODUCTS = [
    ("Polypropylene Granules (PP)", "Petrochemicals"), ("Purified Terephthalic Acid (PTA)", "Petrochemicals"),
    ("Linear Alkyl Benzene (LAB)", "Petrochemicals"), ("Mono Ethylene Glycol (MEG)", "Petrochemicals"),
    ("High-Speed Diesel (HSD)", "Fuel"), ("Aviation Turbine Fuel (ATF)", "Fuel"),
    ("Polyvinyl Chloride (PVC)", "Petrochemicals"), ("Paraxylene (PX)", "Petrochemicals"),
]
PAYMENT_METHODS = ["CREDIT_CARD", "UPI", "NET_BANKING", "DEBIT_CARD", "WIRE_TRANSFER"]


def _seeded_rng(catalog: str) -> random.Random:
    seed = int(hashlib.sha256(catalog.encode()).hexdigest(), 16) % (2**32)
    return random.Random(seed)


def _pan(rng: random.Random) -> str:
    letters1 = "".join(rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ") for _ in range(5))
    digits = "".join(rng.choice("0123456789") for _ in range(4))
    letter2 = rng.choice("ABCDEFGHIJKLMNOPQRSTUVWXYZ")
    return f"{letters1}{digits}{letter2}"


def _aadhaar(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789") for _ in range(12))


def _phone(rng: random.Random) -> str:
    return "+91" + rng.choice("6789") + "".join(rng.choice("0123456789") for _ in range(9))


def _card_number(rng: random.Random) -> str:
    return "".join(rng.choice("0123456789") for _ in range(16))


def _email(first: str, last: str, domain: str, rng: random.Random) -> str:
    return f"{first.lower()}.{last.lower()}{rng.randint(1, 999)}@{domain}"


def sql_str(v) -> str:
    if v is None:
        return "NULL"
    return "'" + str(v).replace("'", "''") + "'"


def generate_dataset(catalog: str):
    """Returns dict of table_name -> list[dict] rows for one catalog."""
    rng = _seeded_rng(catalog)

    customers = []
    for i in range(1, 21):
        bu = "Retail" if i % 2 == 0 else "O2C"
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        city, state = rng.choice(CITIES_STATES)
        domain = "gmail.com" if bu == "Retail" else "ril-o2c-partner.com"
        customers.append({
            "customer_id": i,
            "full_name": f"{first} {last}",
            "email": _email(first, last, domain, rng),
            "phone_number": _phone(rng),
            "pan_number": _pan(rng),
            "aadhaar_number": _aadhaar(rng),
            "address": f"{rng.randint(1, 999)} {rng.choice(['MG Road', 'Ring Road', 'Park Street', 'Church Street', 'Anna Salai'])}",
            "city": city,
            "state": state,
            "business_unit": bu,
            "signup_date": f"2024-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}",
        })

    sales_reps = []
    for i in range(1, 11):
        bu = "Retail" if i % 2 == 0 else "O2C"
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        city, _ = rng.choice(CITIES_STATES)
        sales_reps.append({
            "rep_id": i,
            "rep_name": f"{first} {last}",
            "rep_email": _email(first, last, "ril.com", rng),
            "rep_phone": _phone(rng),
            "business_unit": bu,
            "hire_date": f"202{rng.randint(0,4)}-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}",
            "region": city,
        })

    products = []
    pid = 1
    for name, category in RETAIL_PRODUCTS:
        products.append({
            "product_id": pid, "product_name": name, "category": category,
            "unit_price": round(rng.uniform(199, 49999), 2), "business_unit": "Retail",
        })
        pid += 1
    for name, category in O2C_PRODUCTS:
        products.append({
            "product_id": pid, "product_name": name, "category": category,
            "unit_price": round(rng.uniform(50000, 5000000), 2), "business_unit": "O2C",
        })
        pid += 1

    retail_products = [p for p in products if p["business_unit"] == "Retail"]
    o2c_products = [p for p in products if p["business_unit"] == "O2C"]
    retail_customers = [c for c in customers if c["business_unit"] == "Retail"]
    o2c_customers = [c for c in customers if c["business_unit"] == "O2C"]
    retail_reps = [r for r in sales_reps if r["business_unit"] == "Retail"]
    o2c_reps = [r for r in sales_reps if r["business_unit"] == "O2C"]

    orders = []
    for i in range(1, 31):
        bu = "Retail" if i % 2 == 0 else "O2C"
        cust = rng.choice(retail_customers if bu == "Retail" else o2c_customers)
        rep = rng.choice(retail_reps if bu == "Retail" else o2c_reps)
        prod = rng.choice(retail_products if bu == "Retail" else o2c_products)
        qty = rng.randint(1, 20)
        orders.append({
            "order_id": i,
            "customer_id": cust["customer_id"],
            "rep_id": rep["rep_id"],
            "product_id": prod["product_id"],
            "order_date": f"2025-{rng.randint(1,12):02d}-{rng.randint(1,28):02d}",
            "quantity": qty,
            "order_amount": round(qty * prod["unit_price"], 2),
            "business_unit": bu,
            "shipping_address": f"{rng.randint(1,999)} {cust['city']} Logistics Park, {cust['state']}",
        })

    payments = []
    for i in range(1, 31):
        order = orders[i - 1]
        first, last = rng.choice(FIRST_NAMES), rng.choice(LAST_NAMES)
        payments.append({
            "payment_id": i,
            "order_id": order["order_id"],
            "payment_method": rng.choice(PAYMENT_METHODS),
            "card_number": _card_number(rng),
            "card_holder_name": f"{first} {last}",
            "amount": order["order_amount"],
            "payment_date": order["order_date"],
            "business_unit": order["business_unit"],
        })

    return {
        "customers": customers,
        "sales_reps": sales_reps,
        "products": products,
        "orders": orders,
        "payments": payments,
    }
