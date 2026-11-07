# -*- coding: utf-8 -*-
"""ZivoIT Sandbox carrier: a fully offline simulated courier used for demos, training and tests.

* rates follow a realistic Indian zone model (local / regional / metro / national / special)
* pincodes ending in ``000`` are not serviceable, pincodes starting with ``99`` are COD-blocked
* the AWB encodes the booking time; the shipment progresses one milestone every
  ``extra['step_minutes']`` minutes (default 60). Destination pincodes ending with ``13`` fail
  delivery (NDR) and then go RTO, which makes NDR/RTO flows demo-able.
"""
import hashlib
from datetime import timedelta

from .base import (BaseProvider, LabelResult, ProviderError, RateOption, ServiceabilityResult, ShipmentResult,
                   TrackingEvent, TrackingResult, ceil_slab, register, utcnow)
from ..ai.india import zone_for

ZONE_PRICING = {  # base 0.5 kg, each additional 0.5 kg, tat
    'local': (32.0, 28.0, 1), 'regional': (38.0, 33.0, 2), 'metro': (45.0, 40.0, 3),
    'national': (52.0, 47.0, 5), 'special': (68.0, 60.0, 7),
}
FORWARD_FLOW = [('BKD', 'Shipment booked', 'booked'), ('PKS', 'Pickup scheduled', 'pickup_scheduled'),
                ('PKD', 'Shipment picked up', 'picked_up'), ('ITR', 'In transit to destination hub', 'in_transit'),
                ('OFD', 'Out for delivery', 'out_for_delivery'), ('DLV', 'Delivered to consignee', 'delivered')]
NDR_FLOW = FORWARD_FLOW[:5] + [('NDR', 'Consignee not available - door locked', 'ndr'),
                               ('RTI', 'RTO initiated after 2 attempts', 'rto_initiated'),
                               ('RTT', 'RTO in transit', 'rto_in_transit'), ('RTD', 'Returned to shipper', 'rto_delivered')]
SERVICES = {'STD': ('Sandbox Standard', 1.0, 0), 'EXP': ('Sandbox Express', 1.6, -1)}

def build_label(lines):
    """Render a real 4x6 inch PDF label (reportlab ships with Odoo)."""
    import io
    from reportlab.lib.units import inch
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    pdf = canvas.Canvas(buf, pagesize=(4 * inch, 6 * inch))
    y = 6 * inch - 40
    for i, line in enumerate(lines):
        pdf.setFont('Helvetica-Bold' if i < 2 else 'Helvetica', 14 if i < 2 else 11)
        pdf.drawString(18, y, str(line)[:48])
        y -= 24
    pdf.rect(12, 12, 4 * inch - 24, 6 * inch - 24)
    pdf.showPage()
    pdf.save()
    return buf.getvalue()


@register
class DemoProvider(BaseProvider):
    code = 'demo'
    name = 'ZivoIT Sandbox Courier'
    capabilities = {'serviceability', 'rate', 'create', 'label', 'track', 'cancel', 'pickup', 'ndr', 'webhook'}
    credential_fields = ()
    STATUS_MAP = {code.lower(): norm for code, _d, norm in NDR_FLOW + FORWARD_FLOW}
    STATUS_MAP.update({'cnl': 'cancelled'})

    def _step(self):
        return float((self.config.extra or {}).get('step_minutes', 60))

    def test_connection(self):
        return 'Sandbox courier ready (offline simulation).'

    def check_serviceability(self, pickup_zip, delivery_zip, weight_kg=0.5, cod=False):
        zip_ = str(delivery_zip or '')
        if len(zip_) != 6 or not zip_.isdigit() or zip_.endswith('000'):
            return ServiceabilityResult(zip=zip_, serviceable=False, message='Pincode not serviceable')
        zone = zone_for(pickup_zip, zip_)
        return ServiceabilityResult(zip=zip_, serviceable=True, cod=not zip_.startswith('99'), prepaid=True, pickup=True,
                                    tat_days=ZONE_PRICING[zone][2], message='Zone: %s' % zone)

    def get_rates(self, request):
        svc = self.check_serviceability(request.pickup.zip, request.delivery.zip, request.chargeable_weight, request.is_cod)
        if not svc.serviceable or (request.is_cod and not svc.cod):
            return []
        zone = zone_for(request.pickup.zip, request.delivery.zip)
        base, extra, tat = ZONE_PRICING[zone]
        slabs = ceil_slab(request.chargeable_weight, 0.5) / 0.5
        freight = base + max(slabs - 1, 0) * extra
        cod_fee = max(30.0, 0.015 * request.cod_amount) if request.is_cod else 0.0
        rates = []
        for code, (label, mult, tat_delta) in SERVICES.items():
            price = round(freight * mult * 1.18 + cod_fee, 2)  # incl. 18 % GST
            rates.append(RateOption(service_code=code, service_name=label, courier_name=self.name, price=price,
                                    tat_days=max(tat + tat_delta, 1), cod_available=svc.cod, extra={'zone': zone}))
        return rates

    def create_shipment(self, request):
        if not request.delivery.zip or not request.delivery.clean_phone:
            raise ProviderError('Sandbox: consignee pincode and phone are mandatory.')
        svc = self.check_serviceability(request.pickup.zip, request.delivery.zip)
        if not svc.serviceable:
            raise ProviderError('Sandbox: pincode %s is not serviceable.' % request.delivery.zip)
        now = utcnow()
        digest = hashlib.sha1(('%s%s' % (request.reference, now.isoformat())).encode()).hexdigest()[:4].upper()
        awb = 'ZD%d%s%s' % (int(now.timestamp()), digest, request.delivery.zip[-2:])
        rate = next((r for r in self.get_rates(request) if r.service_code == (request.service_code or 'STD')), None)
        label = build_label(['ZivoIT Sandbox Courier', 'AWB: %s' % awb, 'Ref: %s' % request.reference,
                             'To: %s' % request.delivery.name, '%s %s' % (request.delivery.city, request.delivery.zip),
                             'COD: %.2f' % request.cod_amount if request.is_cod else 'PREPAID'])
        return ShipmentResult(tracking_number=awb, carrier_reference='DEMO-%s' % request.reference,
                              price=rate.price if rate else 0.0, service_code=request.service_code or 'STD',
                              courier_name=self.name, label_pdf=label,
                              edd=now + timedelta(days=rate.tat_days if rate else 4))

    def get_label(self, tracking_number, carrier_reference=''):
        return LabelResult(pdf=build_label(['ZivoIT Sandbox Courier', 'AWB: %s' % tracking_number]))

    def cancel(self, tracking_number, carrier_reference=''):
        status = self._status(tracking_number)
        if status and status.normalized not in ('booked', 'pickup_scheduled'):
            raise ProviderError('Sandbox: shipment already picked up, cannot cancel.')
        return True

    def schedule_pickup(self, tracking_numbers, pickup_date, carrier_references=None, package_count=1):
        return 'PICKUP-%s' % pickup_date.strftime('%Y%m%d')

    def ndr_action(self, tracking_number, action, data=None):
        return True

    def track(self, tracking_numbers):
        return {awb: self._status(awb) or TrackingResult(tracking_number=awb, found=False) for awb in tracking_numbers}

    def _status(self, awb):
        if not awb or not awb.startswith('ZD'):
            return None
        try:
            booked = int(awb[2:12])
        except ValueError:
            return None
        from datetime import datetime, timezone
        booked_at = datetime.fromtimestamp(booked, timezone.utc).replace(tzinfo=None)
        flow = NDR_FLOW if awb.endswith('13') else FORWARD_FLOW
        elapsed = (utcnow() - booked_at).total_seconds() / 60.0
        step = self._step()
        reached = len(flow) if step <= 0 else min(int(elapsed // step) + 1, len(flow))
        events = [TrackingEvent(time=booked_at + timedelta(minutes=step * i), raw_status=code, description=desc,
                                location='Origin Hub' if i < 3 else 'Destination Hub', normalized=norm)
                  for i, (code, desc, norm) in enumerate(flow[:reached])]
        res = self._result_from_events(awb, events, edd=booked_at + timedelta(days=3))
        if res.normalized == 'ndr':
            res.ndr_reason = events[-1].description
        return res

    def parse_webhook(self, payload, headers=None):
        awb = payload.get('awb')
        if not awb:
            return []
        from .base import parse_datetime
        ev = TrackingEvent(time=parse_datetime(payload.get('time')) or utcnow(), raw_status=payload.get('status', ''),
                           description=payload.get('description', ''), location=payload.get('location', ''))
        return [self._result_from_events(awb, [ev], ndr_reason=payload.get('ndr_reason', ''))]
