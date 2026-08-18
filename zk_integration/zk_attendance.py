import frappe
from werkzeug.wrappers import Response
from datetime import datetime

logger = frappe.logger("zk_integration", allow_site=True, file_count=10)


class ADMSRenderer:
	"""Handles ZKTeco ADMS protocol requests under /iclock/*."""

	DEBOUNCE_SECONDS = 30

	def __init__(self, path, status_code=None):
		self.path = path
		self.status_code = status_code or 200

	def can_render(self):
		return "iclock/" in self.path.lower()

	def render(self):
		args = frappe.local.form_dict
		sn = args.get("SN")

		if not self._is_known_device(sn):
			logger.warning(f"Rejected push from unknown/disabled SN={sn}")
			return Response("", mimetype="text/plain", status=403)

		self._touch_last_seen(sn)

		if "getrequest" in self.path.lower():
			return Response("OK", mimetype="text/plain")

		table = args.get("table")
		if table == "ATTLOG":
			raw = frappe.request.get_data(as_text=True).strip()
			self.handle_attlog(sn, raw)

		return Response("OK", mimetype="text/plain")

	def _is_known_device(self, sn):
		if not sn:
			return False
		return frappe.db.exists("Biometric Device", {"serial_number": sn, "enabled": 1})

	def _touch_last_seen(self, sn):
		frappe.db.set_value("Biometric Device", {"serial_number": sn}, "last_seen", frappe.utils.now())
		frappe.db.commit()

	def handle_attlog(self, sn, raw):
		for line in raw.splitlines():
			self._process_line(sn, line)

	def _process_line(self, sn, line):
		parts = line.split()
		if len(parts) < 3:
			logger.warning(f"Malformed ATTLOG line from SN={sn}: {line!r}")
			return

		pin, date, time = parts[0], parts[1], parts[2]
		timestamp = f"{date} {time}"

		try:
			frappe.utils.get_datetime(timestamp)
		except (ValueError, TypeError):
			logger.warning(f"Invalid datetime format from SN={sn}: {timestamp!r}")
			return

		employee = frappe.db.get_value(
			"Employee", {"attendance_device_id": pin},
			["name", "employee_name", "user_id"], as_dict=True,
		)
		if not employee:
			logger.warning(f"No Employee for device pin={pin} (SN={sn})")
			return

		if self._is_bounce(employee['name'], timestamp):
			logger.info(f"Bounce punch ignored (within {self.DEBOUNCE_SECONDS}s): employee={employee['name']} time={timestamp}")
			return

		try:
			doc = frappe.new_doc("Employee Checkin")
			doc.employee = employee['name']
			doc.employee_name = employee['employee_name']
			doc.time = timestamp
			doc.device_id = sn
			doc.insert(ignore_permissions=True)
			owner_user = employee.get('user_id') or ""
			frappe.db.sql(
				"UPDATE `tabEmployee Checkin` SET owner=%s, modified_by=%s WHERE name=%s",
				(owner_user, owner_user, doc.name)
			)
			frappe.db.commit()
		except frappe.DuplicateEntryError:
			frappe.db.rollback()
			logger.info(f"Duplicate checkin ignored: pin={pin} time={timestamp}")
		except Exception:
			frappe.db.rollback()
			logger.error(f"Failed to record checkin pin={pin} time={timestamp}\n{frappe.get_traceback()}")

	def _is_bounce(self, employee, timestamp):
		last_time = frappe.db.get_value(
			"Employee Checkin", {"employee": employee}, "time", order_by="time desc"
		)
		if not last_time:
			return False
		delta = abs((frappe.utils.get_datetime(timestamp) - frappe.utils.get_datetime(last_time)).total_seconds())
		return delta < self.DEBOUNCE_SECONDS