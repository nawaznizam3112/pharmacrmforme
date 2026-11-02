import pandas as pd
import streamlit as st
from datetime import date, timedelta

import db
import services as s

st.set_page_config("PharmaWH", "💊", layout="wide")
db.init()


def df(sql, params=()):
    return pd.DataFrame(db.q(sql, params))


def download(frame, name):
    st.download_button("⬇ CSV", frame.to_csv(index=False), f"{name}.csv", "text/csv", key=f"dl_{name}")


def run(action, ok_msg):
    try:
        action()
        st.success(ok_msg)
        return True
    except Exception as e:  # business-rule errors surface as plain messages
        st.error(str(e))
        return False


st.sidebar.title("💊 PharmaWH")
page = st.sidebar.radio("Menu", ["Dashboard", "Products", "Parties", "Purchase (GRN)", "Sales Invoice",
                                 "Stock & Batches", "Adjustments", "Reports"])
my_state = st.sidebar.text_input("Your state (GST place of supply)", "Karnataka")
if not db.q("SELECT 1 FROM products LIMIT 1") and st.sidebar.button("Load demo data"):
    s.seed_demo()
    st.rerun()

products = db.q("SELECT * FROM products ORDER BY name")
suppliers = db.q("SELECT * FROM suppliers ORDER BY name")
customers = db.q("SELECT * FROM customers ORDER BY name")
pmap = {f"{p['code']} · {p['name']}": p["id"] for p in products}

# ------------------------------------------------------------------ Dashboard
if page == "Dashboard":
    st.title("Dashboard")
    val = db.q("SELECT COALESCE(SUM(qty*cost),0) v FROM batches")[0]["v"]
    near = df(s.EXPIRY, ("+90 days",))
    expired = near[near["days_left"] < 0] if not near.empty else near
    low = df(s.LOW_STOCK)
    today_sales = db.q("SELECT COALESCE(SUM(total),0) t FROM sales WHERE date=?", (date.today().isoformat(),))[0]["t"]
    due = db.q("SELECT COALESCE(SUM(total-paid),0) d FROM sales")[0]["d"]
    c = st.columns(5)
    c[0].metric("Stock value (cost)", f"₹{val:,.0f}")
    c[1].metric("Today's sales", f"₹{today_sales:,.0f}")
    c[2].metric("Receivables", f"₹{due:,.0f}")
    c[3].metric("Expiring ≤90d", len(near) - len(expired))
    c[4].metric("Expired in stock", len(expired))
    l, r = st.columns(2)
    l.subheader("Expiry alerts (90 days)")
    l.dataframe(near, hide_index=True, width='stretch')
    r.subheader("Below reorder level")
    r.dataframe(low, hide_index=True, width='stretch')

# ------------------------------------------------------------------ Products
elif page == "Products":
    st.title("Product master")
    with st.form("prod", clear_on_submit=True):
        a = st.columns(4)
        code, name, generic, mfr = a[0].text_input("Code*"), a[1].text_input("Name*"), a[2].text_input("Generic"), a[3].text_input("Manufacturer")
        b = st.columns(6)
        hsn, gst = b[0].text_input("HSN", "3004"), b[1].selectbox("GST %", [0, 5, 12, 18], index=2)
        sched, pack = b[2].selectbox("Schedule", ["OTC", "H", "H1", "X"]), b[3].text_input("Pack")
        reorder, cold = b[4].number_input("Reorder level", 0, step=10), b[5].checkbox("Cold chain")
        if st.form_submit_button("Add product") and code and name:
            def _add():
                with db.conn() as c:
                    c.execute("INSERT INTO products(code,name,generic,manufacturer,hsn,gst_rate,schedule,pack,reorder_level,cold_chain) "
                              "VALUES(?,?,?,?,?,?,?,?,?,?)", (code, name, generic, mfr, hsn, gst, sched, pack, reorder, int(cold)))
            run(_add, "Product added.")
    search = st.text_input("Search")
    f = df("SELECT * FROM products WHERE name LIKE ? OR code LIKE ? OR generic LIKE ? ORDER BY name", (f"%{search}%",) * 3)
    st.dataframe(f, hide_index=True, width='stretch')
    download(f, "products")

# ------------------------------------------------------------------ Parties
elif page == "Parties":
    st.title("Suppliers & customers")
    t1, t2 = st.tabs(["Customers", "Suppliers"])
    with t1:
        with st.form("cust", clear_on_submit=True):
            a = st.columns(3)
            n, g, stt = a[0].text_input("Name*"), a[1].text_input("GSTIN"), a[2].text_input("State", "Karnataka")
            b = st.columns(3)
            ph, dl, cl = b[0].text_input("Phone"), b[1].text_input("Drug licence no."), b[2].number_input("Credit limit ₹", 0.0, step=5000.0)
            if st.form_submit_button("Add customer") and n:
                with db.conn() as c:
                    c.execute("INSERT INTO customers(name,gstin,state,phone,drug_license,credit_limit) VALUES(?,?,?,?,?,?)",
                              (n, g, stt, ph, dl, cl))
                st.rerun()
        st.dataframe(df("SELECT * FROM customers"), hide_index=True, width='stretch')
    with t2:
        with st.form("sup", clear_on_submit=True):
            a = st.columns(5)
            n, g, stt, ph, dl = (a[0].text_input("Name*"), a[1].text_input("GSTIN"), a[2].text_input("State"),
                                 a[3].text_input("Phone"), a[4].text_input("Drug licence no."))
            if st.form_submit_button("Add supplier") and n:
                with db.conn() as c:
                    c.execute("INSERT INTO suppliers(name,gstin,state,phone,drug_license) VALUES(?,?,?,?,?)", (n, g, stt, ph, dl))
                st.rerun()
        st.dataframe(df("SELECT * FROM suppliers"), hide_index=True, width='stretch')

# ------------------------------------------------------------------ Purchase
elif page == "Purchase (GRN)":
    st.title("Goods receipt (purchase)")
    if not (suppliers and products):
        st.info("Add at least one supplier and one product first.")
        st.stop()
    smap = {x["name"]: x["id"] for x in suppliers}
    c = st.columns(2)
    sup, inv = c[0].selectbox("Supplier", smap), c[1].text_input("Supplier invoice no.*")
    grid = st.data_editor(
        pd.DataFrame({"product": pd.Series(dtype=str), "batch_no": pd.Series(dtype=str),
                      "expiry": pd.Series(dtype="datetime64[ns]"), "qty": pd.Series(dtype=int),
                      "cost": pd.Series(dtype=float), "ptr": pd.Series(dtype=float), "mrp": pd.Series(dtype=float)}),
        num_rows="dynamic", width='stretch', key="grn",
        column_config={"product": st.column_config.SelectboxColumn(options=list(pmap), required=True),
                       "expiry": st.column_config.DateColumn(required=True)})
    if st.button("Post GRN", type="primary"):
        rows = grid.dropna(subset=["product", "batch_no", "expiry", "qty", "cost", "ptr", "mrp"])
        if not inv or rows.empty:
            st.error("Enter the invoice number and at least one complete line.")
        else:
            lines = [dict(product_id=pmap[r.product], batch_no=r.batch_no, expiry=pd.to_datetime(r.expiry).date().isoformat(),
                          qty=int(r.qty), cost=float(r.cost), ptr=float(r.ptr), mrp=float(r.mrp)) for r in rows.itertuples()]
            run(lambda: s.receive_stock(smap[sup], inv, lines), f"GRN posted: {len(lines)} lines, ₹{sum(l['qty']*l['cost'] for l in lines):,.2f}")

# ------------------------------------------------------------------ Sales
elif page == "Sales Invoice":
    st.title("Sales invoice")
    if not (customers and products):
        st.info("Add at least one customer and one product first.")
        st.stop()
    cmap = {x["name"]: x["id"] for x in customers}
    cust = st.selectbox("Customer", cmap)
    st.caption(f"Outstanding: ₹{s.outstanding(cmap[cust]):,.2f}. Batches are auto-picked earliest-expiry-first; "
               "rate defaults to the batch PTR if left blank.")
    grid = st.data_editor(
        pd.DataFrame({"product": pd.Series(dtype=str), "qty": pd.Series(dtype=int),
                      "rate": pd.Series(dtype=float), "disc": pd.Series(dtype=float)}),
        num_rows="dynamic", width='stretch', key="sale",
        column_config={"product": st.column_config.SelectboxColumn(options=list(pmap), required=True),
                       "disc": st.column_config.NumberColumn("disc %", min_value=0, max_value=100)})
    if st.button("Generate invoice", type="primary"):
        rows = grid.dropna(subset=["product", "qty"])
        lines = [dict(product_id=pmap[r.product], qty=int(r.qty), rate=None if pd.isna(r.rate) else float(r.rate),
                      disc=0 if pd.isna(r.disc) else float(r.disc)) for r in rows.itertuples() if r.qty > 0]
        if not lines:
            st.error("Add at least one line.")
        else:
            try:
                st.session_state["last_sale"] = s.create_sale(cmap[cust], lines, my_state)
            except Exception as e:
                st.error(str(e))
    sid = st.session_state.get("last_sale")
    if sid:
        h, lines = s.get_invoice(sid)
        st.subheader(f"{h['invoice_no']} · {h['customer']} · {h['date']}")
        inv_df = pd.DataFrame(lines)
        st.dataframe(inv_df, hide_index=True, width='stretch')
        tax = h["tax"]
        cols = st.columns(4)
        cols[0].metric("Taxable", f"₹{h['subtotal']:,.2f}")
        if h["interstate"]:
            cols[1].metric("IGST", f"₹{tax:,.2f}")
        else:
            cols[1].metric("CGST", f"₹{tax/2:,.2f}")
            cols[2].metric("SGST", f"₹{tax/2:,.2f}")
        cols[3].metric("Total", f"₹{h['total']:,.2f}")
        download(inv_df, h["invoice_no"])
    st.divider()
    st.subheader("Receive payment")
    open_inv = df("SELECT s.id, s.invoice_no, c.name customer, s.total, s.paid, ROUND(s.total-s.paid,2) due "
                  "FROM sales s JOIN customers c ON c.id=s.customer_id WHERE s.total-s.paid>0.005 ORDER BY s.id DESC")
    if open_inv.empty:
        st.caption("No outstanding invoices.")
    else:
        st.dataframe(open_inv.drop(columns="id"), hide_index=True, width='stretch')
        a = st.columns(3)
        pick = a[0].selectbox("Invoice", open_inv["invoice_no"])
        row = open_inv[open_inv["invoice_no"] == pick].iloc[0]
        amt = a[1].number_input("Amount ₹", 0.0, float(row["due"]), float(row["due"]))
        if a[2].button("Record payment"):
            if run(lambda: s.receive_payment(int(row["id"]), amt), "Payment recorded."):
                st.rerun()

# ------------------------------------------------------------------ Stock
elif page == "Stock & Batches":
    st.title("Stock & batches")
    f = df("SELECT p.code, p.name, p.schedule, p.cold_chain, b.id batch_id, b.batch_no, b.expiry, b.qty, b.cost, b.ptr, b.mrp, "
           "CAST(julianday(b.expiry)-julianday('now') AS INT) days_left FROM batches b JOIN products p ON p.id=b.product_id "
           "WHERE b.qty>0 ORDER BY b.expiry")
    if not f.empty:
        f["status"] = f["days_left"].apply(lambda d: "EXPIRED" if d < 0 else "≤90d" if d <= 90 else "OK")
        sel = st.multiselect("Status", ["EXPIRED", "≤90d", "OK"], default=["EXPIRED", "≤90d", "OK"])
        f = f[f["status"].isin(sel)]
    st.dataframe(f, hide_index=True, width='stretch')
    download(f, "stock")
    st.subheader("Movement ledger")
    st.dataframe(df("SELECT l.ts, p.name, b.batch_no, l.change, l.reason, l.ref FROM ledger l "
                    "JOIN products p ON p.id=l.product_id JOIN batches b ON b.id=l.batch_id ORDER BY l.id DESC LIMIT 200"),
                 hide_index=True, width='stretch')

# ------------------------------------------------------------------ Adjustments
elif page == "Adjustments":
    st.title("Stock adjustments")
    bt = db.q("SELECT b.id, p.name, b.batch_no, b.expiry, b.qty FROM batches b JOIN products p ON p.id=b.product_id ORDER BY p.name")
    opts = {f"{b['name']} · {b['batch_no']} · exp {b['expiry']} · qty {b['qty']}": b["id"] for b in bt}
    with st.form("adj"):
        pick = st.selectbox("Batch", opts)
        reason = st.selectbox("Reason", s.REASONS)
        change = st.number_input("Quantity change (use negative to remove)", value=0, step=1)
        if st.form_submit_button("Apply") and change != 0:
            run(lambda: s.adjust_stock(opts[pick], int(change), reason), "Adjustment recorded.")

# ------------------------------------------------------------------ Reports
else:
    st.title("Reports")
    a = st.columns(2)
    d1 = a[0].date_input("From", date.today() - timedelta(days=30))
    d2 = a[1].date_input("To", date.today())
    reports = {
        "Stock valuation": (s.VALUATION, ()),
        "Expiry (next 180 days)": (s.EXPIRY, ("+180 days",)),
        "Low stock": (s.LOW_STOCK, ()),
        "Daily sales": (s.DAILY_SALES, (str(d1), str(d2))),
        "Top products & margin": (s.TOP_PRODUCTS, (str(d1), str(d2))),
        "GST summary (outward)": (s.GST, (str(d1), str(d2))),
        "Receivables": (s.RECEIVABLES, ()),
    }
    for tab, (name, (sql, params)) in zip(st.tabs(list(reports)), reports.items()):
        with tab:
            r = df(sql, params)
            st.dataframe(r, hide_index=True, width='stretch')
            if not r.empty:
                download(r, name.split()[0].lower())
