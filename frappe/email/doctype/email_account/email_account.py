# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and contributors
# For license information, please see license.txt

from __future__ import unicode_literals
import frappe
import imaplib
import re
import socket
from frappe import _
from frappe.model.document import Document
from frappe.utils import validate_email_add, cint, get_datetime, DATE_FORMAT, strip, comma_or, sanitize_html
from frappe.utils.user import is_system_user
from frappe.utils.jinja import render_template
from frappe.email.smtp import SMTPServer
from frappe.email.receive import EmailServer, Email
from poplib import error_proto
from dateutil.relativedelta import relativedelta
from datetime import datetime, timedelta
from frappe.desk.form import assign_to
from frappe.utils.user import get_system_managers
from frappe.core.doctype.communication.email import set_incoming_outgoing_accounts
from frappe.utils.error import make_error_snapshot
from frappe.email import set_customer_supplier




class SentEmailInInbox(Exception): pass

class EmailAccount(Document):
	def autoname(self):
		"""Set name as `email_account_name` or make title from email id."""
		if not self.email_account_name:
			self.email_account_name = self.email_id.split("@", 1)[0]\
				.replace("_", " ").replace(".", " ").replace("-", " ").title()

			if self.service:
				self.email_account_name = self.email_account_name + " " + self.service

		self.name = self.email_account_name

	def validate(self):
		"""Validate email id and check POP3/IMAP and SMTP connections is enabled."""
		if self.email_id:
			validate_email_add(self.email_id, True)

		if self.login_id_is_different:
			if not self.login_id:
				frappe.throw(_("Login Id is required"))
		else:
			self.login_id = None

		if frappe.local.flags.in_patch or frappe.local.flags.in_test:
			return

		#if self.enable_incoming and not self.append_to:
		#	frappe.throw(_("Append To is mandatory for incoming mails"))

		if not self.awaiting_password and not frappe.local.flags.in_install and not frappe.local.flags.in_patch:
			if self.enable_incoming:
				self.get_server()


			if self.enable_outgoing:
				self.check_smtp()

		if self.notify_if_unreplied:
			if not self.send_notification_to:
				frappe.throw(_("{0} is mandatory").format(self.meta.get_label("send_notification_to")))
			for e in self.get_unreplied_notification_emails():
				validate_email_add(e, True)

		if self.enable_incoming and self.append_to:
			valid_doctypes = [d[0] for d in get_append_to()]
			if self.append_to not in valid_doctypes:
				frappe.throw(_("Append To can be one of {0}").format(comma_or(valid_doctypes)))

		if self.awaiting_password:
			# push values to user_emails
			frappe.db.sql("""update `tabUser Emails` set awaiting_password = 1
						  where email_account = %(account)s""", {"account": self.name})
		else:
			frappe.db.sql("""update `tabUser Emails` set awaiting_password = 0
									  where email_account = %(account)s""", {"account": self.name})

		from frappe.email import ask_pass_update
		ask_pass_update()

	def on_update(self):
		"""Check there is only one default of each type."""
		self.there_must_be_only_one_default()

	def there_must_be_only_one_default(self):
		"""If current Email Account is default, un-default all other accounts."""
		for fn in ("default_incoming", "default_outgoing"):
			if self.get(fn):
				for email_account in frappe.get_all("Email Account",
					filters={fn: 1}):
					if email_account.name==self.name:
						continue
					email_account = frappe.get_doc("Email Account",
						email_account.name)
					email_account.set(fn, 0)
					email_account.save()

	@frappe.whitelist()
	def get_domain(self,email_id):
		"""look-up the domain and then full"""
		try:
			domain = email_id.split("@")
			return frappe.db.sql("""select name,use_imap,email_server,use_ssl,smtp_server,use_tls,smtp_port
			from tabDomain
			where name = %s
			""",domain[1],as_dict=1)
		except Exception:
			pass

	def check_smtp(self):
		"""Checks SMTP settings."""
		if self.enable_outgoing:
			if not self.smtp_server:
				frappe.throw(_("{0} is required").format("SMTP Server"))

			server = SMTPServer(login = getattr(self, "login_id", None) \
					or self.email_id,
				password = self.password,
				server = self.smtp_server,
				port = cint(self.smtp_port),
				use_ssl = cint(self.use_tls)
			)
			server.sess

	def get_server(self, in_receive=False):
		"""Returns logged in POP3 connection object."""
		args = {
			"email_account":self.name,
			"host": self.email_server,
			"use_ssl": self.use_ssl,
			"username": getattr(self, "login_id", None) or self.email_id,
			"password": self.password,
			"use_imap": self.use_imap,
			"uid_validity":self.uid_validity
		}

		if not args.get("host"):
			frappe.throw(_("{0} is required").format("Email Server"))

		email_server = EmailServer(frappe._dict(args))
		try:
			email_server.connect()
		except (error_proto, imaplib.IMAP4.error), e:
			if in_receive and ("authentication failed" in e.message.lower() or "log in via your web browser" in e.message.lower()):
				# if called via self.receive and it leads to authentication error, disable incoming
				# and send email to system manager
				self.handle_incoming_connect_error(
					description=_('Authentication failed while receiving emails from Email Account {0}. Message from server: {1}'.format(self.name, e.message))
				)

				return None

			else:
				frappe.throw(e.message)

		except socket.error:
			if in_receive:
				# timeout while connecting, see receive.py connect method
				description = frappe.message_log.pop() if frappe.message_log else "Socket Error"
				self.handle_incoming_connect_error(description=description)

				return None

			else:
				raise
		if not in_receive:
			if self.use_imap:
				email_server.imap.logout()
		return email_server

	def handle_incoming_connect_error(self, description):
		self.db_set("enable_incoming", 0)

		for user in get_system_managers(only_name=True):
			try:
				assign_to.add({
					'assign_to': user,
					'doctype': self.doctype,
					'name': self.name,
					'description': description,
					'priority': 'High',
					'notify': 1
				})
			except assign_to.DuplicateToDoError:
				frappe.message_log.pop()
				pass


	def receive(self, test_mails=None):
		"""Called by scheduler to receive emails from this EMail account using POP3/IMAP."""
		import time
		print('starting'+self.email_account_name)
		self.time =[]
		self.time.append(time.time())
		if self.enable_incoming:
			if frappe.local.flags.in_test:
				incoming_mails = test_mails
			else:
				email_server = self.get_server(in_receive=True)
				if not email_server:
					return
				self.time.append(time.time())
				incoming_mails = email_server.get_messages()
				self.time.append(time.time())

			exceptions = []

			for raw,uid,seen in incoming_mails:
				try:

					communication = self.insert_communication(raw,uid,seen)
					#self.notify_update()

				except SentEmailInInbox,e:
					frappe.db.rollback()
					make_error_snapshot(e)
					self.handle_bad_emails(email_server, uid, raw,"sent email in inbox")


				except Exception, e:
					frappe.db.rollback()
					make_error_snapshot(e)
					self.handle_bad_emails(email_server, uid, raw,frappe.get_traceback())
					exceptions.append(frappe.get_traceback())

				else:
					frappe.db.commit()
					attachments = [d.file_name for d in communication._attachments]
					communication.notify(attachments=attachments, fetched_from_email_account=True)

			#update attachment folder size as suspended for emails
			try:
				folder = frappe.get_doc("File", 'Home/Attachments')
				folder.save()
			except:
				exceptions.append(frappe.get_traceback())

			#notify if user is linked to account
			if len(incoming_mails)>0:
				frappe.publish_realtime('new_email', {"account":self.email_account_name,"number":len(incoming_mails)})

			self.time.append(time.time())
			print (self.email_account_name+': end sync setup;fetch;parse {0},{1},{2}={3}'.format(round(self.time[1]-self.time[0],2),round(self.time[2]-self.time[1],2),round(self.time[3]-self.time[2],2),round(self.time[3]-self.time[0],2)))

			if exceptions:
				raise Exception, frappe.as_json(exceptions)

	def handle_bad_emails(self,email_server,uid,raw,reason):
		if cint(email_server.settings.use_imap):
			import email
			try:
				mail = email.message_from_string(raw)
				message_id = mail.__getitem__('Message-ID')
			except Exception:
				message_id = "can't be parsed"

			unhandled_email = frappe.get_doc({
				"doctype": "Unhandled Emails",
				"email_account": email_server.settings.email_account,
				"uid": uid,
				"message_id": message_id,
				"reason":reason
			})
			unhandled_email.save();
			frappe.db.commit();

	def insert_communication(self, raw,uid,seen):
		email = Email(raw)

		if email.from_email == self.email_id and not email.mail.get("Reply-To"):
			# gmail shows sent emails in inbox
			# and we don't want emails sent by us to be pulled back into the system again
			# dont count emails sent by the system get those
			raise SentEmailInInbox
		contact = set_customer_supplier(email.from_email,email.To)

		communication = frappe.get_doc({
			"doctype": "Communication",
			"subject": email.subject,
			"content": email.content,
			"sent_or_received": "Received",
			"sender_full_name": email.from_real_name,
			"sender": email.from_email,
			"recipients": email.To,
			"cc": email.CC,
			"email_account": self.name,
			"communication_medium": "Email",
			"timeline_doctype":contact["timeline_doctype"],
			"timeline_name":contact["timeline_name"],
			"timeline_label":contact["timeline_label"],
			"uid":uid,
			"message_id":email.message_id,
			"actualdate":email.date,
			"has_attachment": 1 if email.attachments else 0,
			"seen":seen
		})

		self.set_thread(communication, email)

		communication.flags.in_receive = True
		communication.insert(ignore_permissions = 1)

		# save attachments
		communication._attachments = email.save_attachments_in_doc(communication)

		# replace inline images


		dirty = False
		for file in communication._attachments:
			if file.name in email.cid_map and email.cid_map[file.name]:
				dirty = True

				email.content = email.content.replace("cid:{0}".format(email.cid_map[file.name]),
					file.file_url)

		if dirty:
			# not sure if using save() will trigger anything
			communication.db_set("content", sanitize_html(email.content))

		# notify all participants of this thread
		if self.enable_auto_reply and getattr(communication, "is_first", False):
			self.send_auto_reply(communication, email)

		return communication

	def set_thread(self, communication, email):
		"""Appends communication to parent based on thread ID. Will extract
		parent communication and will link the communication to the reference of that
		communication. Also set the status of parent transaction to Open or Replied.

		If no thread id is found and `append_to` is set for the email account,
		it will create a new parent transaction (e.g. Issue)"""
		in_reply_to = (email.mail.get("In-Reply-To") or "").strip(" <>")
		parent = None

		if self.append_to:
			# set subject_field and sender_field
			meta_module = frappe.get_meta_module(self.append_to)
			meta = frappe.get_meta(self.append_to)
			subject_field = getattr(meta_module, "subject_field", "subject")
			if not meta.get_field(subject_field):
				subject_field = None
			sender_field = getattr(meta_module, "sender_field", "sender")
			if not meta.get_field(sender_field):
				sender_field = None

		if in_reply_to:
			if "{0}".format(frappe.local.site) in in_reply_to:

				# reply to a communication sent from the system
				in_reply_to, domain = in_reply_to.split("@", 1)

				if frappe.db.exists("Communication", in_reply_to):
					parent = frappe.get_doc("Communication", in_reply_to)

					# set in_reply_to of current communication
					communication.in_reply_to = in_reply_to

					if parent.reference_name:
						parent = frappe.get_doc(parent.reference_doctype,
							parent.reference_name)

		if not parent and self.append_to and sender_field:
			if subject_field:
				# try and match by subject and sender
				# if sent by same sender with same subject,
				# append it to old coversation
				subject = strip(re.sub("^\s*(Re|RE)[^:]*:\s*", "", email.subject))

				parent = frappe.db.get_all(self.append_to, filters={
					sender_field: email.from_email,
					subject_field: ("like", "%{0}%".format(subject)),
					"creation": (">", (get_datetime() - relativedelta(days=10)).strftime(DATE_FORMAT))
				}, fields="name")

				# match only subject field
				# when the from_email is of a user in the system
				# and subject is atleast 10 chars long
				if not parent and len(subject) > 10 and is_system_user(email.from_email):
					parent = frappe.db.get_all(self.append_to, filters={
						subject_field: ("like", "%{0}%".format(subject)),
						"creation": (">", (get_datetime() - relativedelta(days=10)).strftime(DATE_FORMAT))
					}, fields="name")

			if parent:
				parent = frappe.get_doc(self.append_to, parent[0].name)

		if not parent:
			# try match doctype based on subject
			if ':' in email.subject:
				try:
					subject = strip(re.sub("(^\s*(Fw|FW|fwd)[^:]*:|\s*(Re|RE)[^:]*:\s*)*","", email.subject))
					if ':' in subject:
						reference_doctype,reference_name = subject.split(': ',1)
						parent = frappe.get_doc(reference_doctype,reference_name)
				except:
					pass

		if not parent and self.append_to and self.append_to!="Communication":
			# no parent found, but must be tagged
			# insert parent type doc
			parent = frappe.new_doc(self.append_to)

			if subject_field:
				parent.set(subject_field, email.subject)

			if sender_field:
				parent.set(sender_field, email.from_email)

			parent.flags.ignore_mandatory = True

			try:
				parent.insert(ignore_permissions=True)
			except frappe.DuplicateEntryError:
				# try and find matching parent
				parent_name = frappe.db.get_value(self.append_to, {sender_field: email.from_email})
				if parent_name:
					parent.name = parent_name
				else:
					parent = None

			# NOTE if parent isn't found and there's no subject match, it is likely that it is a new conversation thread and hence is_first = True
			communication.is_first = True

		if parent:
			communication.reference_doctype = parent.doctype
			communication.reference_name = parent.name

	def send_auto_reply(self, communication, email):
		"""Send auto reply if set."""
		if self.enable_auto_reply:
			set_incoming_outgoing_accounts(communication)

			frappe.sendmail(recipients = [email.from_email],
				sender = self.email_id,
				reply_to = communication.incoming_email_account,
				subject = _("Re: ") + communication.subject,
				content = render_template(self.auto_reply_message or "", communication.as_dict()) or \
					 frappe.get_template("templates/emails/auto_reply.html").render(communication.as_dict()),
				reference_doctype = communication.reference_doctype,
				reference_name = communication.reference_name,
				message_id = communication.name,
				in_reply_to = email.mail.get("Message-Id"), # send back the Message-Id as In-Reply-To
				unsubscribe_message = _("Leave this conversation"),
				bulk=True)

	def get_unreplied_notification_emails(self):
		"""Return list of emails listed"""
		self.send_notification_to = self.send_notification_to.replace(",", "\n")
		out = [e.strip() for e in self.send_notification_to.split("\n") if e.strip()]
		return out

	def on_trash(self):
		"""Clear communications where email account is linked"""
		frappe.db.sql("update `tabCommunication` set email_account='' where email_account=%s", self.name)

	def after_rename(self, old, new, merge=False):
		frappe.db.set_value("Email Account", new, "email_account_name", new)

@frappe.whitelist()
def get_append_to(doctype=None, txt=None, searchfield=None, start=None, page_len=None, filters=None):
	if not txt: txt = ""
	return [[d] for d in frappe.get_hooks("email_append_to") if txt in d]

def pull(now=False):
	"""Will be called via scheduler, pull emails from all enabled Email accounts."""
	import frappe.tasks
	for email_account in frappe.get_list("Email Account", filters={"enable_incoming": 1,"awaiting_password": 0}):
		if now:
			frappe.tasks.pull_from_email_account(frappe.local.site, email_account.name)
		else:
			frappe.tasks.pull_from_email_account.delay(frappe.local.site, email_account.name)

def notify_unreplied():
	"""Sends email notifications if there are unreplied Communications
		and `notify_if_unreplied` is set as true."""

	for email_account in frappe.get_all("Email Account", "name", filters={"enable_incoming": 1, "notify_if_unreplied": 1}):
		email_account = frappe.get_doc("Email Account", email_account.name)
		if email_account.append_to:

			# get open communications younger than x mins, for given doctype
			for comm in frappe.get_all("Communication", "name", filters={
					"sent_or_received": "Received",
					"reference_doctype": email_account.append_to,
					"unread_notification_sent": 0,
					"creation": ("<", datetime.now() - timedelta(seconds = (email_account.unreplied_for_mins or 30) * 60)),
					"creation": (">", datetime.now() - timedelta(seconds = (email_account.unreplied_for_mins or 30) * 60 * 3))
				}):
				comm = frappe.get_doc("Communication", comm.name)

				if frappe.db.get_value(comm.reference_doctype, comm.reference_name, "status")=="Open":
					# if status is still open
					frappe.sendmail(recipients=email_account.get_unreplied_notification_emails(),
						content=comm.content, subject=comm.subject, doctype= comm.reference_doctype,
						name=comm.reference_name, bulk=True)

				# update flag
				comm.db_set("unread_notification_sent", 1)
