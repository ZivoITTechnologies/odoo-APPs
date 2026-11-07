# -*- coding: utf-8 -*-
import math

from markupsafe import Markup, escape

from odoo import api, fields, models, _
from odoo.exceptions import UserError

PALETTE = ['#2E7D6B', '#C9783B', '#4A6FA5', '#9C5A9E', '#B8A03A', '#5B8C3A', '#B5525B', '#3F8FA0']


class CpLoadPlan(models.Model):
    _name = 'cp.load.plan'
    _description = 'Load Plan (3D Load Builder)'
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char(default=lambda self: _('New'), copy=False, readonly=True)
    vehicle_type_id = fields.Many2one('cp.vehicle.type', required=True, tracking=True)
    indent_id = fields.Many2one('cp.indent')
    trip_id = fields.Many2one('cp.trip')
    item_ids = fields.One2many('cp.load.plan.item', 'plan_id', string='Items', copy=True)
    box_ids = fields.One2many('cp.load.plan.box', 'plan_id', string='Placements', readonly=True)
    state = fields.Selection([('draft', 'Draft'), ('planned', 'Planned')], default='draft')
    weight_kg = fields.Float(readonly=True, string='Loaded Weight (kg)')
    volume_cbm = fields.Float(readonly=True, string='Loaded Volume (m³)', digits=(12, 3))
    weight_util = fields.Float(readonly=True, string='Weight Utilisation %')
    volume_util = fields.Float(readonly=True, string='Volume Utilisation %')
    front_axle_pct = fields.Float(readonly=True, string='Front Share of Load %',
                                  help='Share of the cargo weight carried by the front half of the deck. Aim for 40-60%.')
    unplaced_count = fields.Integer(readonly=True)
    preview_svg = fields.Html(readonly=True, sanitize=False, string='3D Preview')
    company_id = fields.Many2one('res.company', default=lambda self: self.env.company)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', _('New')) == _('New'):
                vals['name'] = self.env['ir.sequence'].next_by_code('cp.load.plan') or _('New')
        return super().create(vals_list)

    # ------------------------------------------------------------------
    # Packing: column stacking inside slices along the deck length.
    # Items for the last drop are loaded first (deepest), so unloading
    # never needs to move another customer's cargo.
    # ------------------------------------------------------------------
    def action_compute(self):
        for plan in self:
            plan._cp_pack()
        return True

    def _cp_expand_items(self):
        boxes = []
        for idx, item in enumerate(self.item_ids):
            for _n in range(int(item.quantity)):
                dims = sorted([item.length_m, item.width_m])
                # Lay the longer side along the deck length.
                boxes.append({'item': item, 'l': dims[1], 'w': dims[0], 'h': item.height_m,
                              'kg': item.weight_kg, 'stack': item.stackable,
                              'drop': item.drop_sequence, 'color': PALETTE[idx % len(PALETTE)]})
        boxes.sort(key=lambda b: (-b['drop'], -(b['l'] * b['w']), -b['h']))
        return boxes

    def _cp_pack(self):
        self.ensure_one()
        vt = self.vehicle_type_id
        if not self.item_ids:
            raise UserError(_('Add the items to load first.'))
        L, W, H = vt.length_m, vt.width_m, vt.height_m
        cap_kg = vt.capacity_ton * 1000.0
        columns = []  # each: {x, y, l, w, top, stackable, drop}
        slice_x, slice_depth, cursor_y = 0.0, 0.0, 0.0
        placed, unplaced, total_kg = [], 0, 0.0
        eps = 1e-6
        for box in self._cp_expand_items():
            if total_kg + box['kg'] > cap_kg + eps:
                unplaced += 1
                continue
            target = None
            for col in columns:
                if (col['stackable'] and box['stack'] and col['drop'] == box['drop']
                        and box['l'] <= col['l'] + eps and box['w'] <= col['w'] + eps
                        and col['top'] + box['h'] <= H + eps):
                    target = col
                    break
            if target is None:
                if box['h'] > H + eps or box['w'] > W + eps:
                    unplaced += 1
                    continue
                if cursor_y + box['w'] > W + eps:
                    slice_x += slice_depth
                    cursor_y, slice_depth = 0.0, 0.0
                if slice_x + box['l'] > L + eps:
                    unplaced += 1
                    continue
                target = {'x': slice_x, 'y': cursor_y, 'l': box['l'], 'w': box['w'], 'top': 0.0,
                          'stackable': box['stack'], 'drop': box['drop']}
                columns.append(target)
                cursor_y += box['w']
                slice_depth = max(slice_depth, box['l'])
            placed.append(dict(box, x=target['x'], y=target['y'], z=target['top']))
            target['top'] += box['h']
            total_kg += box['kg']

        volume = sum(b['l'] * b['w'] * b['h'] for b in placed)
        # Cargo nearer the cab (x small) loads the front axle.
        front_kg = sum(b['kg'] * max(0.0, 1 - (b['x'] + b['l'] / 2) / L) for b in placed) if L else 0.0
        self.box_ids.unlink()
        self.write({
            'box_ids': [(0, 0, {'item_id': b['item'].id, 'x': b['x'], 'y': b['y'], 'z': b['z'],
                                'length_m': b['l'], 'width_m': b['w'], 'height_m': b['h']}) for b in placed],
            'weight_kg': total_kg,
            'volume_cbm': volume,
            'weight_util': (total_kg / cap_kg * 100.0) if cap_kg else 0.0,
            'volume_util': (volume / vt.volume_cbm * 100.0) if vt.volume_cbm else 0.0,
            'front_axle_pct': (front_kg / total_kg * 100.0) if total_kg else 0.0,
            'unplaced_count': unplaced,
            'state': 'planned',
            'preview_svg': self._cp_render_svg(placed, L, W, H),
        })
        if unplaced:
            self.message_post(body=_('%s item(s) did not fit by size or weight.', unplaced))

    def _cp_render_svg(self, placed, L, W, H):
        """Isometric drawing of the deck and placed boxes."""
        scale = 520.0 / max(L + W, 1.0)
        cos30, sin30 = math.cos(math.radians(30)), 0.5

        def proj(x, y, z):
            return ((x - y) * cos30 * scale, ((x + y) * sin30 - z) * scale)

        corners = [proj(0, 0, 0), proj(L, 0, 0), proj(L, W, 0), proj(0, W, 0), proj(0, 0, H), proj(0, W, H)]
        xs, ys = [c[0] for c in corners], [c[1] for c in corners]
        minx, miny = min(xs) - 20, min(ys) - 34
        width, height = max(xs) - minx + 20, max(ys) - miny + 20

        def pt(x, y, z):
            px, py = proj(x, y, z)
            return '%.1f,%.1f' % (px - minx, py - miny)

        parts = [
            '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %.0f %.0f" class="cp-load-svg" '
            'style="max-width:100%%;height:auto">' % (width, height),
            # deck floor, cab wall and side wall outlines
            '<polygon points="%s %s %s %s" fill="#eef1f4" stroke="#8a96a3" stroke-width="1"/>'
            % (pt(0, 0, 0), pt(L, 0, 0), pt(L, W, 0), pt(0, W, 0)),
            '<polygon points="%s %s %s %s" fill="none" stroke="#b7c0c9" stroke-dasharray="4 3"/>'
            % (pt(0, 0, 0), pt(0, W, 0), pt(0, W, H), pt(0, 0, H)),
            '<polygon points="%s %s %s %s" fill="none" stroke="#b7c0c9" stroke-dasharray="4 3"/>'
            % (pt(0, 0, 0), pt(L, 0, 0), pt(L, 0, H), pt(0, 0, H)),
        ]
        # Painter's order: far boxes first.
        for b in sorted(placed, key=lambda b: (b['x'], b['y'], b['z'])):
            x0, y0, z0 = b['x'], b['y'], b['z']
            x1, y1, z1 = x0 + b['l'], y0 + b['w'], z0 + b['h']
            color = b['color']
            title = escape(b['item'].name or '')
            parts.append('<g><title>%s</title>' % title)
            parts.append('<polygon points="%s %s %s %s" fill="%s" stroke="#1f2a33" stroke-width="0.6"/>'
                         % (pt(x0, y0, z1), pt(x1, y0, z1), pt(x1, y1, z1), pt(x0, y1, z1), color))
            parts.append('<polygon points="%s %s %s %s" fill="%s" fill-opacity="0.78" stroke="#1f2a33" '
                         'stroke-width="0.6"/>' % (pt(x1, y0, z0), pt(x1, y1, z0), pt(x1, y1, z1), pt(x1, y0, z1), color))
            parts.append('<polygon points="%s %s %s %s" fill="%s" fill-opacity="0.6" stroke="#1f2a33" '
                         'stroke-width="0.6"/>' % (pt(x0, y1, z0), pt(x1, y1, z0), pt(x1, y1, z1), pt(x0, y1, z1), color))
            parts.append('</g>')
        cab = proj(0, 0, H)
        parts.append('<text x="%.1f" y="%.1f" font-size="12" fill="#55606b" text-anchor="middle">%s</text>'
                     % (cab[0] - minx, cab[1] - miny - 8, escape(_('Cab end'))))
        parts.append('</svg>')
        return Markup(''.join(parts))


class CpLoadPlanItem(models.Model):
    _name = 'cp.load.plan.item'
    _description = 'Load Plan Item'

    plan_id = fields.Many2one('cp.load.plan', required=True, ondelete='cascade')
    name = fields.Char(required=True)
    quantity = fields.Integer(default=1)
    length_m = fields.Float(string='L (m)', default=1.2, digits=(6, 3))
    width_m = fields.Float(string='W (m)', default=1.0, digits=(6, 3))
    height_m = fields.Float(string='H (m)', default=1.0, digits=(6, 3))
    weight_kg = fields.Float(string='Weight / unit (kg)', default=500.0)
    stackable = fields.Boolean(default=True)
    drop_sequence = fields.Integer(string='Drop #', default=1,
                                   help='Delivery order. The last drop is loaded first, deepest in the truck.')


class CpLoadPlanBox(models.Model):
    _name = 'cp.load.plan.box'
    _description = 'Placed Box'
    _order = 'x, y, z'

    plan_id = fields.Many2one('cp.load.plan', required=True, ondelete='cascade')
    item_id = fields.Many2one('cp.load.plan.item', ondelete='cascade')
    x = fields.Float(string='From Cab (m)', digits=(6, 2))
    y = fields.Float(string='From Left (m)', digits=(6, 2))
    z = fields.Float(string='From Floor (m)', digits=(6, 2))
    length_m = fields.Float(digits=(6, 2))
    width_m = fields.Float(digits=(6, 2))
    height_m = fields.Float(digits=(6, 2))
