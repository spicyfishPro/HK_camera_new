import json
from core.field_tag_lookup import parse_field_tag_response


def tag_payload(*, entry=1, plan=1, row=None, number='A1', name='', population_id=1,
                group='群体甲', code=None, ck=False):
    return dict(schema_version=2, code=code or f'P{entry}', planting_entry_id=entry,
        planting_plan_id=plan, current_material_no='CK' if ck else number,
        row_no=str(row if row is not None else entry), year=2026, location='鸡泽', is_ck=ck,
        entry_type='ck' if ck else 'accession',
        global_material=None if ck else {'id':'00000000-0000-4000-8000-000000000001','name':name,'is_active':True},
        membership={'id':population_id,'accession_no':number,'population_id':population_id,'is_active':True} if population_id is not None and not ck else None,
        planting_population={'id':population_id,'name':group,'display':group,'is_active':True} if population_id is not None else None,
        section={'id':population_id or 99,'label':group,'replicate':None,'description':''},
        source_reference={'legacy_accession_id':99,'confirmed_accession_id':None,'basis':''},
        photographer={'id':1,'display_name':'张三'},
        material={'id':99,'accession_no':'旧编号','name':'旧备注'}, population={'id':99,'name':'旧群体'})


def tag_info(**kwargs):
    return parse_field_tag_response(json.dumps(tag_payload(**kwargs)))
