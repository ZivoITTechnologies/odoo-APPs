# -*- coding: utf-8 -*-


def post_init_hook(env):
    """Switch on the India compliance features when a company is located in India."""
    if any(company.country_id.code == 'IN' for company in env['res.company'].search([])):
        env['res.company']._jw_in_enable()
