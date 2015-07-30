# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt

from __future__ import unicode_literals
import frappe

from frappe.utils import flt, cstr, nowdate, nowtime
from erpnext.stock.utils import update_bin
from erpnext.stock.stock_ledger import update_entries_after
from erpnext.accounts.utils import get_fiscal_year

def repost(only_actual=False, allow_negative_stock=False, allow_zero_rate=False):
	"""
	Repost everything!
	"""
	frappe.db.auto_commit_on_many_writes = 1

	if allow_negative_stock:
		existing_allow_negative_stock = frappe.db.get_value("Stock Settings", None, "allow_negative_stock")
		frappe.db.set_value("Stock Settings", None, "allow_negative_stock", 1)

	for d in frappe.db.sql("""select distinct item_code, warehouse from
		(select item_code, warehouse from tabBin
		union
		select item_code, warehouse from `tabStock Ledger Entry`) a"""):
			try:
				repost_stock(d[0], d[1], allow_zero_rate, only_actual)
				frappe.db.commit()
			except:
				frappe.db.rollback()

	if allow_negative_stock:
		frappe.db.set_value("Stock Settings", None, "allow_negative_stock", existing_allow_negative_stock)
	frappe.db.auto_commit_on_many_writes = 0

def repost_stock(item_code, warehouse, allow_zero_rate=False, only_actual=False):
	repost_actual_qty(item_code, warehouse, allow_zero_rate)

	if item_code and warehouse and not only_actual:
		update_bin_qty(item_code, warehouse, {
			"reserved_qty": get_reserved_qty(item_code, warehouse),
			"indented_qty": get_indented_qty(item_code, warehouse),
			"ordered_qty": get_ordered_qty(item_code, warehouse),
			"planned_qty": get_planned_qty(item_code, warehouse)
		})

def repost_actual_qty(item_code, warehouse, allow_zero_rate=False):
	try:
		update_entries_after({ "item_code": item_code, "warehouse": warehouse }, allow_zero_rate)
	except:
		pass

def get_reserved_qty(item_code, warehouse):
	reserved_qty = frappe.db.sql("""
		select
			sum((dnpi_qty / so_item_qty) * (so_item_qty - so_item_delivered_qty))
		from
			(
				(select
					qty as dnpi_qty,
					(
						select qty from `tabSales Order Item`
						where name = dnpi.parent_detail_docname
					) as so_item_qty,
					(
						select ifnull(delivered_qty, 0) from `tabSales Order Item`
						where name = dnpi.parent_detail_docname
					) as so_item_delivered_qty,
					parent, name
				from
				(
					select qty, parent_detail_docname, parent, name
					from `tabPacked Item` dnpi_in
					where item_code = %s and warehouse = %s
					and parenttype="Sales Order"
					and item_code != parent_item
					and exists (select * from `tabSales Order` so
					where name = dnpi_in.parent and docstatus = 1 and status != 'Stopped')
				) dnpi)
			union
				(select qty as dnpi_qty, qty as so_item_qty,
					ifnull(delivered_qty, 0) as so_item_delivered_qty, parent, name
				from `tabSales Order Item` so_item
				where item_code = %s and warehouse = %s
				and exists(select * from `tabSales Order` so
					where so.name = so_item.parent and so.docstatus = 1
					and so.status != 'Stopped'))
			) tab
		where
			so_item_qty >= so_item_delivered_qty
	""", (item_code, warehouse, item_code, warehouse))

	return flt(reserved_qty[0][0]) if reserved_qty else 0

def get_indented_qty(item_code, warehouse):
	indented_qty = frappe.db.sql("""select sum(mr_item.qty - ifnull(mr_item.ordered_qty, 0))
		from `tabMaterial Request Item` mr_item, `tabMaterial Request` mr
		where mr_item.item_code=%s and mr_item.warehouse=%s
		and mr_item.qty > ifnull(mr_item.ordered_qty, 0) and mr_item.parent=mr.name
		and mr.status!='Stopped' and mr.docstatus=1""", (item_code, warehouse))

	return flt(indented_qty[0][0]) if indented_qty else 0

def get_ordered_qty(item_code, warehouse):
	ordered_qty = frappe.db.sql("""
		select sum((po_item.qty - ifnull(po_item.received_qty, 0))*po_item.conversion_factor)
		from `tabPurchase Order Item` po_item, `tabPurchase Order` po
		where po_item.item_code=%s and po_item.warehouse=%s
		and po_item.qty > ifnull(po_item.received_qty, 0) and po_item.parent=po.name
		and po.status!='Stopped' and po.docstatus=1""", (item_code, warehouse))

	return flt(ordered_qty[0][0]) if ordered_qty else 0

def get_planned_qty(item_code, warehouse):
	planned_qty = frappe.db.sql("""
		select sum(ifnull(qty, 0) - ifnull(produced_qty, 0)) from `tabProduction Order`
		where production_item = %s and fg_warehouse = %s and status != "Stopped"
		and docstatus=1 and ifnull(qty, 0) > ifnull(produced_qty, 0)""", (item_code, warehouse))

	return flt(planned_qty[0][0]) if planned_qty else 0


def update_bin_qty(item_code, warehouse, qty_dict=None):
	from erpnext.stock.utils import get_bin
	bin = get_bin(item_code, warehouse)
	mismatch = False
	for fld, val in qty_dict.items():
		if flt(bin.get(fld)) != flt(val):
			bin.set(fld, flt(val))
			mismatch = True

	if mismatch:
		bin.projected_qty = flt(bin.actual_qty) + flt(bin.ordered_qty) + \
			flt(bin.indented_qty) + flt(bin.planned_qty) - flt(bin.reserved_qty)

		bin.save()

def set_stock_balance_as_per_serial_no(item_code=None, posting_date=None, posting_time=None,
	 	fiscal_year=None):
	if not posting_date: posting_date = nowdate()
	if not posting_time: posting_time = nowtime()
	if not fiscal_year: fiscal_year = get_fiscal_year(posting_date)[0]

	condition = " and item.name='%s'" % item_code.replace("'", "\'") if item_code else ""

	bin = frappe.db.sql("""select bin.item_code, bin.warehouse, bin.actual_qty, item.stock_uom
		from `tabBin` bin, tabItem item
		where bin.item_code = item.name and item.has_serial_no = 1 %s""" % condition)

	for d in bin:
		serial_nos = frappe.db.sql("""select count(name) from `tabSerial No`
			where item_code=%s and warehouse=%s and status = 'Available' and docstatus < 2""", (d[0], d[1]))

		if serial_nos and flt(serial_nos[0][0]) != flt(d[2]):
			print d[0], d[1], d[2], serial_nos[0][0]

		sle = frappe.db.sql("""select valuation_rate, company from `tabStock Ledger Entry`
			where item_code = %s and warehouse = %s and ifnull(is_cancelled, 'No') = 'No'
			order by posting_date desc limit 1""", (d[0], d[1]))

		sle_dict = {
			'doctype'					: 'Stock Ledger Entry',
			'item_code'					: d[0],
			'warehouse'					: d[1],
			'transaction_date'	 		: nowdate(),
			'posting_date'				: posting_date,
			'posting_time'			 	: posting_time,
			'voucher_type'			 	: 'Stock Reconciliation (Manual)',
			'voucher_no'				: '',
			'voucher_detail_no'			: '',
			'actual_qty'				: flt(serial_nos[0][0]) - flt(d[2]),
			'stock_uom'					: d[3],
			'incoming_rate'				: sle and flt(serial_nos[0][0]) > flt(d[2]) and flt(sle[0][0]) or 0,
			'company'					: sle and cstr(sle[0][1]) or 0,
			'fiscal_year'				: fiscal_year,
			'is_cancelled'			 	: 'No',
			'batch_no'					: '',
			'serial_no'					: ''
		}

		sle_doc = frappe.get_doc(sle_dict)
		sle_doc.flags.ignore_validate = True
		sle_doc.flags.ignore_links = True
		sle_doc.insert()

		args = sle_dict.copy()
		args.update({
			"sle_id": sle_doc.name,
			"is_amended": 'No'
		})

		update_bin(args)
		update_entries_after({
			"item_code": d[0],
			"warehouse": d[1],
			"posting_date": posting_date,
			"posting_time": posting_time
		})

def reset_serial_no_status_and_warehouse(serial_nos=None):
	if not serial_nos:
		serial_nos = frappe.db.sql_list("""select name from `tabSerial No` where status != 'Not in Use'
			and docstatus = 0""")
		for serial_no in serial_nos:
			try:
				sr = frappe.get_doc("Serial No", serial_no)
				last_sle = sr.get_last_sle()
				if flt(last_sle.actual_qty) > 0:
					sr.warehouse = last_sle.warehouse

				sr.via_stock_ledger = True
				sr.save()
			except:
				pass

		frappe.db.sql("""update `tabSerial No` set warehouse='' where status in ('Delivered', 'Purchase Returned')""")

def repost_all_stock_vouchers():
	warehouses_with_account = frappe.db.sql_list("""select master_name from tabAccount
		where ifnull(account_type, '') = 'Warehouse'""")

	vouchers = frappe.db.sql("""select distinct voucher_type, voucher_no
		from `tabStock Ledger Entry` sle
		where voucher_type != "Serial No" and sle.warehouse in (%s)
		order by posting_date, posting_time, name""" %
		', '.join(['%s']*len(warehouses_with_account)), tuple(warehouses_with_account))

	rejected = []
	i = 0
	for voucher_type, voucher_no in vouchers:
		i+=1
		print i, "/", len(vouchers)
		try:
			for dt in ["Stock Ledger Entry", "GL Entry"]:
				frappe.db.sql("""delete from `tab%s` where voucher_type=%s and voucher_no=%s"""%
					(dt, '%s', '%s'), (voucher_type, voucher_no))

			doc = frappe.get_doc(voucher_type, voucher_no)
			if voucher_type=="Stock Entry" and doc.purpose in ["Manufacture", "Repack"]:
				doc.get_stock_and_rate(force=1)
			elif voucher_type=="Purchase Receipt" and doc.is_subcontracted == "Yes":
				doc.validate()

			doc.update_stock_ledger()
			doc.make_gl_entries(repost_future_gle=False)
			frappe.db.commit()
		except Exception, e:
			print frappe.get_traceback()
			rejected.append([voucher_type, voucher_no])
			frappe.db.rollback()

	print rejected
