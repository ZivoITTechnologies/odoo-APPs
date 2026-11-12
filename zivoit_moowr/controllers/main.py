from odoo import http
from odoo.http import content_disposition, request


class MoowrController(http.Controller):

    @http.route('/moowr/return/<int:return_id>/xlsx', type='http', auth='user')
    def moowr_return_xlsx(self, return_id, **kwargs):
        ret = request.env['moowr.return'].browse(return_id).exists()
        if not ret:
            return request.not_found()
        ret.check_access('read')
        content = ret._generate_xlsx()
        filename = f"MOOWR_Return_{ret.licence_id.name}_{ret.date_from.strftime('%Y_%m')}.xlsx".replace('/', '-').replace(' ', '_')
        return request.make_response(content, headers=[
            ('Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'),
            ('Content-Disposition', content_disposition(filename)),
        ])
