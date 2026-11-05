# -*- coding: utf-8 -*-
from odoo import _, fields, models
from odoo.exceptions import UserError


class AccountMove(models.Model):
    _inherit = 'account.move'

    tally_state = fields.Selection([
        ('queued', 'Queued for Tally'), ('synced', 'In Tally'), ('error', 'Tally error'),
        ('held', 'Held by AI guard'), ('imported', 'From Tally'), ('modified', 'Changed after sync'),
        ('cancelled', 'Cancelled in Tally'), ('deleted', 'Deleted in Tally')],
        copy=False, tracking=True, index=True)
    tally_connection_id = fields.Many2one('tally.connection', copy=False)
    tally_guid = fields.Char(copy=False, index=True)
    tally_master_id = fields.Integer(copy=False)
    tally_number = fields.Char('Tally voucher no.', copy=False, index=True)
    tally_error = fields.Text(copy=False)
    tally_last_sync = fields.Datetime(copy=False)
    tally_voucher_id = fields.Many2one('tally.voucher', copy=False)
    tally_risk_score = fields.Integer('AI risk score', copy=False)

    def _tally_connections(self):
        return self.env['tally.connection'].sudo().search(
            [('company_id', 'in', self.company_id.ids), ('transport', '!=', False)])

    def _post(self, soft=True):
        posted = super()._post(soft=soft)
        if self.env.context.get('tally_no_push'):
            return posted
        for conn in posted._tally_connections().filtered('push_on_post'):
            moves = posted.filtered(lambda m: m.company_id == conn.company_id
                                    and m.tally_state != 'imported')
            if moves:
                conn._push_moves(moves)
                if conn.transport == 'direct' and not self.env.context.get('tally_defer'):
                    # run after commit so a Tally outage never blocks posting
                    self.env.cr.postcommit.add(
                        lambda cid=conn.id, db=self.env.cr.dbname, uid=self.env.uid:
                        self._tally_postcommit(db, uid, cid))
        return posted

    @staticmethod
    def _tally_postcommit(db, uid, conn_id):
        # executed in a fresh cursor once the posting transaction is committed
        from odoo import api
        from odoo.modules.registry import Registry
        with Registry(db).cursor() as cr:
            env = api.Environment(cr, uid, {})
            conn = env['tally.connection'].sudo().browse(conn_id)
            if conn.exists():
                env['tally.request'].sudo()._process_queue(conn)

    def button_draft(self):
        synced = self.filtered(lambda m: m.tally_state == 'synced')
        res = super().button_draft()
        if not self.env.context.get('tally_no_push'):
            synced.write({'tally_state': 'modified'})
        return res

    def button_cancel(self):
        to_cancel = self.filtered(lambda m: m.tally_guid and m.tally_state in ('synced', 'modified')
                                  and m.tally_connection_id)
        res = super().button_cancel()
        if not self.env.context.get('tally_no_push'):
            for mv in to_cancel:
                mv.tally_connection_id.sudo()._push_cancel(mv)
        return res

    def action_tally_push(self):
        conns = self._tally_connections()
        if not conns:
            raise UserError(_('No Tally connection is configured for this company.'))
        self.filtered(lambda m: m.tally_state == 'deleted').write({'tally_guid': False})
        for conn in conns:
            moves = self.filtered(lambda m: m.company_id == conn.company_id)
            conn._push_moves(moves, force=True)
            self.env['tally.request'].sudo()._process_queue(conn)
        return True

    def action_tally_view_voucher(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'res_model': 'tally.voucher',
                'res_id': self.tally_voucher_id.id, 'view_mode': 'form'}
