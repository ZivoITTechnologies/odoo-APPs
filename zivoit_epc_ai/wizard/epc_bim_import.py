import base64
import re
from collections import OrderedDict

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

# Physical IFC elements that become material lines. Spatial/annotation entities are ignored.
IFC_ELEMENTS = {
    "IFCBEAM", "IFCCOLUMN", "IFCSLAB", "IFCWALL", "IFCWALLSTANDARDCASE", "IFCFOOTING", "IFCPILE", "IFCMEMBER",
    "IFCPLATE", "IFCSTAIR", "IFCSTAIRFLIGHT", "IFCRAMP", "IFCRAILING", "IFCROOF", "IFCCOVERING", "IFCDOOR",
    "IFCWINDOW", "IFCCURTAINWALL", "IFCBUILDINGELEMENTPROXY", "IFCREINFORCINGBAR", "IFCREINFORCINGMESH",
    "IFCTENDON", "IFCPIPESEGMENT", "IFCPIPEFITTING", "IFCDUCTSEGMENT", "IFCDUCTFITTING", "IFCCABLESEGMENT",
    "IFCCABLECARRIERSEGMENT", "IFCCABLECARRIERFITTING", "IFCFLOWSEGMENT", "IFCFLOWFITTING", "IFCFLOWCONTROLLER",
    "IFCFLOWTERMINAL", "IFCFLOWMOVINGDEVICE", "IFCFLOWSTORAGEDEVICE", "IFCENERGYCONVERSIONDEVICE", "IFCVALVE",
    "IFCPUMP", "IFCTANK", "IFCFAN", "IFCCOMPRESSOR", "IFCTRANSFORMER", "IFCELECTRICAPPLIANCE",
    "IFCDISTRIBUTIONCONTROLELEMENT", "IFCFASTENER", "IFCMECHANICALFASTENER", "IFCDISCRETEACCESSORY",
    "IFCEARTHWORKSFILL", "IFCPAVEMENT", "IFCCOURSE", "IFCKERB", "IFCRAIL",
}
STATEMENT = re.compile(r"#(\d+)\s*=\s*([A-Z0-9_]+)\s*\((.*)\)\s*$", re.DOTALL)
TRAILING_NUMBER = re.compile(r"[\s_:#-]*\d+$")


def split_args(text):
    """Split a STEP argument list at top-level commas; nested lists come back as python lists."""
    args, in_string, i = [], False, 0
    stack = [args]
    token = ""
    while i < len(text):
        ch = text[i]
        if in_string:
            if ch == "'":
                if i + 1 < len(text) and text[i + 1] == "'":
                    token += "'"
                    i += 1
                else:
                    in_string = False
                    stack[-1].append(("str", token))
                    token = ""
            else:
                token += ch
        elif ch == "'":
            in_string = True
            token = ""
        elif ch == "(":
            new = []
            stack[-1].append(new)
            stack.append(new)
            token = ""
        elif ch == ")":
            if token.strip():
                stack[-1].append(token.strip())
            token = ""
            if len(stack) > 1:
                stack.pop()
        elif ch == ",":
            if token.strip():
                stack[-1].append(token.strip())
            token = ""
        else:
            token += ch
        i += 1
    if token.strip():
        stack[-1].append(token.strip())
    return args


def _text(value):
    return value[1] if isinstance(value, tuple) and value[0] == "str" else ""


def _ref(value):
    return int(value[1:]) if isinstance(value, str) and value.startswith("#") and value[1:].isdigit() else None


def _number(value):
    if isinstance(value, str):
        match = re.search(r"-?\d+(?:\.\d*)?(?:[eE][-+]?\d+)?", value)
        if match:
            return float(match.group(0))
    return 0.0


def parse_ifc(content):
    """Return an ordered dict {(ifc_type, type_name): {"count", "volume", "area", "length", "weight"}}."""
    if "ISO-10303-21" not in content[:200] and "FILE_SCHEMA" not in content[:2000]:
        raise ValueError("not an IFC (STEP) file")
    data_start = content.find("DATA;")
    body = content[data_start + 5:] if data_start >= 0 else content
    entities = {}
    for raw in re.split(r";\s*(?=#\d+\s*=|ENDSEC)", body):
        match = STATEMENT.match(raw.strip().rstrip(";"))
        if match:
            entities[int(match.group(1))] = (match.group(2).upper(), split_args(match.group(3)))

    # element -> quantities
    quantities = {}
    for _eid, (etype, args) in entities.items():
        if etype != "IFCRELDEFINESBYPROPERTIES" or len(args) < 6:
            continue
        definition = entities.get(_ref(args[5]))
        if not definition or definition[0] != "IFCELEMENTQUANTITY":
            continue
        qty_refs = definition[1][5] if len(definition[1]) > 5 and isinstance(definition[1][5], list) else []
        measures = {}
        for qref in qty_refs:
            quantity = entities.get(_ref(qref))
            if not quantity or len(quantity[1]) < 4:
                continue
            kind = {"IFCQUANTITYVOLUME": "volume", "IFCQUANTITYAREA": "area", "IFCQUANTITYLENGTH": "length",
                    "IFCQUANTITYWEIGHT": "weight"}.get(quantity[0])
            if not kind:
                continue
            name = _text(quantity[1][0]).lower()
            value = _number(quantity[1][3])
            # prefer net quantities over gross ones
            if kind not in measures or name.startswith("net"):
                measures[kind] = value
        for element in args[4] if isinstance(args[4], list) else []:
            ref = _ref(element)
            if ref is not None:
                quantities.setdefault(ref, {}).update(measures)

    groups = OrderedDict()
    for eid, (etype, args) in sorted(entities.items()):
        if etype not in IFC_ELEMENTS:
            continue
        type_name = _text(args[4]) if len(args) > 4 else ""
        if not type_name:
            type_name = TRAILING_NUMBER.sub("", _text(args[2]) if len(args) > 2 else "") or "-"
        key = (etype, type_name)
        group = groups.setdefault(key, {"count": 0, "volume": 0.0, "area": 0.0, "length": 0.0, "weight": 0.0})
        group["count"] += 1
        for kind, value in quantities.get(eid, {}).items():
            group[kind] += value
    return groups


def ifc_label(ifc_type):
    label = ifc_type[3:].replace("STANDARDCASE", "")
    words = {"BUILDINGELEMENTPROXY": "Building element", "PIPESEGMENT": "Pipe", "PIPEFITTING": "Pipe fitting",
             "DUCTSEGMENT": "Duct", "DUCTFITTING": "Duct fitting", "CABLESEGMENT": "Cable",
             "CABLECARRIERSEGMENT": "Cable tray", "CABLECARRIERFITTING": "Cable tray fitting",
             "REINFORCINGBAR": "Rebar", "REINFORCINGMESH": "Mesh", "STAIRFLIGHT": "Stair flight",
             "FLOWSEGMENT": "Flow segment", "FLOWFITTING": "Flow fitting", "CURTAINWALL": "Curtain wall"}
    return words.get(label, label.capitalize())


class EpcBimImport(models.TransientModel):
    _name = "epc.bim.import"
    _description = "Import BIM model (IFC) into Material Take-Off"

    project_id = fields.Many2one("project.project", required=True, domain=[("is_epc", "=", True)])
    document_id = fields.Many2one("epc.document", string="Model Document", domain="[('project_id', '=', project_id)]")
    ifc_file = fields.Binary(string="IFC file", required=True)
    ifc_filename = fields.Char()
    summary = fields.Html(readonly=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if self.env.context.get("active_model") == "project.project" and self.env.context.get("active_id"):
            values.setdefault("project_id", self.env.context["active_id"])
        return values

    @api.model
    def _measure(self, group):
        if group["volume"]:
            return round(group["volume"], 3), "m3"
        if group["weight"]:
            return round(group["weight"], 2), "kg"
        if group["length"]:
            return round(group["length"], 2), "m"
        if group["area"]:
            return round(group["area"], 2), "m2"
        return group["count"], "Nos"

    def action_import(self):
        self.ensure_one()
        raw = base64.b64decode(self.ifc_file)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            content = raw.decode("latin-1")
        try:
            groups = parse_ifc(content)
        except ValueError as exc:
            raise UserError(_("This is not an IFC (STEP) file.")) from exc
        if not groups:
            raise UserError(_("No physical building elements were found in this IFC model."))
        Mto = self.env["epc.mto.line"]
        domain = [("project_id", "=", self.project_id.id), ("source", "=", "bim"), ("state", "=", "draft"),
                  ("document_id", "=", self.document_id.id or False)]
        Mto.search(domain).unlink()
        boq_by_name = {b.name.lower(): b for b in self.project_id.epc_boq_line_ids}
        vals, rows = [], []
        for seq, ((ifc_type, type_name), group) in enumerate(groups.items(), start=1):
            quantity, unit = self._measure(group)
            label = ifc_label(ifc_type)
            description = "%s %s" % (label, type_name) if type_name != "-" else label
            if unit != "Nos":
                description += _(" (%s nos)", group["count"])
            boq = next((b for name, b in boq_by_name.items() if type_name.lower() in name and type_name != "-"), False)
            vals.append({
                "project_id": self.project_id.id, "document_id": self.document_id.id, "sequence": seq * 10,
                "item_code": type_name[:64] if type_name != "-" else ifc_type[3:], "description": description[:200],
                "quantity": quantity, "uom_name": unit, "boq_line_id": boq.id if boq else False, "source": "bim",
            })
            rows.append(Markup("<tr><td>%s</td><td>%s</td><td style='text-align:right'>%s</td><td>%s</td></tr>") % (
                label, type_name, quantity, unit))
        Mto.create(vals)
        self.summary = Markup("<p>%s</p><table class='table table-sm'><tr><th>%s</th><th>%s</th><th>%s</th>"
                              "<th>%s</th></tr>%s</table>") % (
            _("%(lines)s material lines created from %(elements)s model elements.", lines=len(vals),
              elements=sum(g["count"] for g in groups.values())),
            _("Element"), _("Type"), _("Quantity"), _("Unit"), Markup("").join(rows))
        return {
            "type": "ir.actions.act_window",
            "name": _("Material Take-Off"),
            "res_model": "epc.mto.line",
            "view_mode": "list,form",
            "domain": [("project_id", "=", self.project_id.id), ("source", "=", "bim")],
            "context": {"default_project_id": self.project_id.id},
        }
