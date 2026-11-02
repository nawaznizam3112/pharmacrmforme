"""Business rules: GRN, FEFO sales, credit limits, scheduled-drug checks, adjustments."""
from datetime import date

from db import conn, q

CONTROLLED = {"H", "H1", "X"}  # need a drug licence on the buyer
REASONS = ["Expiry write-off", "Damage / breakage", "Count correction",
           "Return to supplier", "Sales return"]


def receive_stock(supplier_id, invoice_no, lines):
    """lines: dicts with product_id, batch_no, expiry (YYYY-MM-DD), qty, cost, ptr, mrp."""
    with conn() as c:
        total = sum(l["qty"] * l["cost"] for l in lines)
        pid = c.execute("INSERT INTO purchases(supplier_id,invoice_no,date,total) VALUES(?,?,?,?)",
                        (supplier_id, invoice_no, date.today().isoformat(), total)).lastrowid
        for l in lines:
            row = c.execute("SELECT id FROM batches WHERE product_id=? AND batch_no=?",
                            (l["product_id"], l["batch_no"])).fetchone()
            if row:
                bid = row["id"]
                c.execute("UPDATE batches SET qty=qty+?, cost=?, ptr=?, mrp=? WHERE id=?",
                          (l["qty"], l["cost"], l["ptr"], l["mrp"], bid))
            else:
                bid = c.execute(
                    "INSERT INTO batches(product_id,batch_no,expiry,qty,cost,ptr,mrp,supplier_id) VALUES(?,?,?,?,?,?,?,?)",
                    (l["product_id"], l["batch_no"], l["expiry"], l["qty"], l["cost"], l["ptr"], l["mrp"],
                     supplier_id)).lastrowid
            c.execute("INSERT INTO ledger(product_id,batch_id,change,reason,ref) VALUES(?,?,?,?,?)",
                      (l["product_id"], bid, l["qty"], "Purchase", invoice_no))
    return pid


def outstanding(customer_id):
    return q("SELECT COALESCE(SUM(total-paid),0) d FROM sales WHERE customer_id=?", (customer_id,))[0]["d"]


def create_sale(customer_id, lines, my_state):
    """lines: dicts product_id, qty, optional rate, disc. Allocates batches First-Expiry-First-Out.
    Atomic: any ValueError rolls back the whole invoice."""
    today = date.today().isoformat()
    with conn() as c:
        cust = c.execute("SELECT * FROM customers WHERE id=?", (customer_id,)).fetchone()
        alloc = []
        for l in lines:
            p = c.execute("SELECT * FROM products WHERE id=?", (l["product_id"],)).fetchone()
            if p["schedule"] in CONTROLLED and not cust["drug_license"]:
                raise ValueError(f"{p['name']} is Schedule {p['schedule']}: customer has no drug licence on file.")
            need = l["qty"]
            batches = c.execute("SELECT * FROM batches WHERE product_id=? AND qty>0 AND expiry>=? "
                                "ORDER BY expiry, id", (p["id"], today)).fetchall()
            for b in batches:
                if need <= 0:
                    break
                take = min(need, b["qty"])
                need -= take
                alloc.append((p, b, take, l.get("rate") or b["ptr"], l.get("disc", 0) or 0))
            if need > 0:
                raise ValueError(f"Insufficient non-expired stock for {p['name']} (short by {need}).")
        subtotal = sum(t * r * (1 - d / 100) for _, _, t, r, d in alloc)
        tax = sum(t * r * (1 - d / 100) * p["gst_rate"] / 100 for p, _, t, r, d in alloc)
        total = round(subtotal + tax, 2)
        if cust["credit_limit"] and outstanding(customer_id) + total > cust["credit_limit"]:
            raise ValueError(f"Credit limit exceeded (limit {cust['credit_limit']:.0f}, "
                             f"outstanding {outstanding(customer_id):.0f}).")
        nxt = c.execute("SELECT COALESCE(MAX(id),0)+1 n FROM sales").fetchone()["n"]
        inv = f"INV-{date.today().year}-{nxt:05d}"
        interstate = int((cust["state"] or "").strip().lower() != my_state.strip().lower())
        sid = c.execute("INSERT INTO sales(invoice_no,customer_id,date,subtotal,tax,total,interstate) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (inv, customer_id, today, round(subtotal, 2), round(tax, 2), total, interstate)).lastrowid
        for p, b, t, r, d in alloc:
            c.execute("INSERT INTO sale_items(sale_id,product_id,batch_id,qty,rate,disc_pct,gst_rate,amount) "
                      "VALUES(?,?,?,?,?,?,?,?)", (sid, p["id"], b["id"], t, r, d, p["gst_rate"],
                                                  round(t * r * (1 - d / 100), 2)))
            c.execute("UPDATE batches SET qty=qty-? WHERE id=?", (t, b["id"]))
            c.execute("INSERT INTO ledger(product_id,batch_id,change,reason,ref) VALUES(?,?,?,?,?)",
                      (p["id"], b["id"], -t, "Sale", inv))
    return sid


def get_invoice(sale_id):
    h = q("SELECT s.*, c.name customer, c.gstin, c.state FROM sales s "
          "JOIN customers c ON c.id=s.customer_id WHERE s.id=?", (sale_id,))[0]
    lines = q("SELECT p.name, p.hsn, b.batch_no, b.expiry, si.qty, si.rate, si.disc_pct, si.gst_rate, si.amount "
              "FROM sale_items si JOIN products p ON p.id=si.product_id JOIN batches b ON b.id=si.batch_id "
              "WHERE si.sale_id=?", (sale_id,))
    return h, lines


def receive_payment(sale_id, amount):
    with conn() as c:
        s = c.execute("SELECT total, paid FROM sales WHERE id=?", (sale_id,)).fetchone()
        if amount <= 0 or amount > s["total"] - s["paid"] + 0.005:
            raise ValueError("Amount must be positive and not exceed the balance due.")
        c.execute("UPDATE sales SET paid=paid+? WHERE id=?", (amount, sale_id))


def adjust_stock(batch_id, change, reason):
    with conn() as c:
        b = c.execute("SELECT product_id FROM batches WHERE id=?", (batch_id,)).fetchone()
        c.execute("UPDATE batches SET qty=qty+? WHERE id=?", (change, batch_id))  # CHECK blocks negatives
        c.execute("INSERT INTO ledger(product_id,batch_id,change,reason,ref) VALUES(?,?,?,?,?)",
                  (b["product_id"], batch_id, change, reason, "manual"))


# ---------- report queries ----------
LOW_STOCK = ("SELECT p.code, p.name, p.reorder_level, COALESCE(SUM(b.qty),0) stock FROM products p "
             "LEFT JOIN batches b ON b.product_id=p.id AND b.expiry>=date('now') GROUP BY p.id "
             "HAVING p.reorder_level>0 AND stock<=p.reorder_level ORDER BY stock")
EXPIRY = ("SELECT p.code, p.name, b.batch_no, b.expiry, b.qty, b.qty*b.cost AS value_at_risk, "
          "CAST(julianday(b.expiry)-julianday('now') AS INT) days_left FROM batches b "
          "JOIN products p ON p.id=b.product_id WHERE b.qty>0 AND b.expiry<=date('now',?) ORDER BY b.expiry")
VALUATION = ("SELECT p.code, p.name, b.batch_no, b.expiry, b.qty, b.cost, b.qty*b.cost AS value "
             "FROM batches b JOIN products p ON p.id=b.product_id WHERE b.qty>0 ORDER BY p.name, b.expiry")
GST = ("SELECT si.gst_rate, ROUND(SUM(si.amount),2) taxable, ROUND(SUM(si.amount*si.gst_rate/100),2) tax "
       "FROM sale_items si JOIN sales s ON s.id=si.sale_id WHERE s.date BETWEEN ? AND ? GROUP BY si.gst_rate")
RECEIVABLES = ("SELECT c.name, ROUND(SUM(s.total-s.paid),2) due FROM sales s JOIN customers c ON c.id=s.customer_id "
               "GROUP BY c.id HAVING due>0.005 ORDER BY due DESC")
TOP_PRODUCTS = ("SELECT p.name, SUM(si.qty) units, ROUND(SUM(si.amount),2) revenue, "
                "ROUND(SUM(si.amount - si.qty*b.cost),2) gross_margin FROM sale_items si "
                "JOIN products p ON p.id=si.product_id JOIN batches b ON b.id=si.batch_id "
                "JOIN sales s ON s.id=si.sale_id WHERE s.date BETWEEN ? AND ? GROUP BY p.id ORDER BY revenue DESC")
DAILY_SALES = ("SELECT date, COUNT(*) invoices, ROUND(SUM(total),2) total FROM sales "
               "WHERE date BETWEEN ? AND ? GROUP BY date ORDER BY date")


def seed_demo():
    with conn() as c:
        c.executemany("INSERT INTO suppliers(name,gstin,state,phone,drug_license) VALUES(?,?,?,?,?)",
                      [("Sun Distributors", "29AAAAA0000A1Z5", "Karnataka", "9800000001", "KA-WH-1001")])
        c.executemany("INSERT INTO customers(name,gstin,state,phone,drug_license,credit_limit) VALUES(?,?,?,?,?,?)",
                      [("City Pharmacy", "29BBBBB1111B1Z5", "Karnataka", "9800000002", "KA-RT-2002", 100000),
                       ("Hyderabad Meds", "36CCCCC2222C1Z5", "Telangana", "9800000003", "TS-RT-3003", 50000),
                       ("Walk-in Store", "", "Karnataka", "", "", 20000)])
        c.executemany("INSERT INTO products(code,name,generic,manufacturer,hsn,gst_rate,schedule,pack,reorder_level,cold_chain) "
                      "VALUES(?,?,?,?,?,?,?,?,?,?)",
                      [("P001", "Paracetamol 500mg", "Paracetamol", "Acme", "3004", 12, "OTC", "10x10", 200, 0),
                       ("P002", "Amoxicillin 500mg", "Amoxicillin", "Acme", "3004", 12, "H", "10x10", 100, 0),
                       ("P003", "Insulin Glargine", "Insulin", "BioCo", "3004", 5, "H", "1x3ml", 20, 1),
                       ("P004", "Alprazolam 0.5mg", "Alprazolam", "NeuroCo", "3004", 12, "H1", "10x10", 30, 0)])
    from datetime import timedelta
    d = lambda n: (date.today() + timedelta(days=n)).isoformat()
    receive_stock(1, "SUP-001", [
        dict(product_id=1, batch_no="PCM-A1", expiry=d(60), qty=300, cost=8, ptr=10.5, mrp=15),
        dict(product_id=1, batch_no="PCM-B2", expiry=d(500), qty=500, cost=8.5, ptr=11, mrp=15),
        dict(product_id=2, batch_no="AMX-77", expiry=d(300), qty=150, cost=45, ptr=58, mrp=80),
        dict(product_id=3, batch_no="INS-09", expiry=d(120), qty=25, cost=520, ptr=610, mrp=750),
        dict(product_id=4, batch_no="ALP-31", expiry=d(250), qty=60, cost=18, ptr=26, mrp=40)])
