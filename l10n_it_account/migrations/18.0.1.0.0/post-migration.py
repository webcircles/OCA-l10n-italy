from openupgradelib import openupgrade
from psycopg2 import sql

from odoo import SUPERUSER_ID, api

from odoo.addons.l10n_it_account.migration_tools import (
    _remove_module,
    remove_modules_views,
)

OLD_MODULES = [
    "account_vat_period_end_statement",
    "l10n_it_account_tax_kind",
    "l10n_it_declaration_of_intent",
    "l10n_it_fatturapa",
    "l10n_it_fatturapa_pec",
    "l10n_it_vat_statement_communication",
]

# Old OCA modules superseded in v18: split payment and reverse charge are now
# handled by Odoo core (l10n_it / account). We migrate no data for them here --
# we only drop their now-dangling views and uninstall them.
OLD_MODULES_TO_REMOVE = [
    "l10n_it_reverse_charge_start_end_dates",
    "l10n_it_reverse_charge",
    "l10n_it_split_payment",
]

# Old modules migrated here but uninstalled by their v18 replacement module
# (marked "to install" by the per-module migration functions below), so we must
# not remove them here:
#   account_vat_period_end_statement    -> l10n_it_account_vat_period_end_settlement
#   l10n_it_declaration_of_intent       -> l10n_it_edi_doi_extension
#   l10n_it_fatturapa                   -> l10n_it_edi_extension
#   l10n_it_fatturapa_pec               -> l10n_it_edi_pec
#   l10n_it_vat_statement_communication -> l10n_it_vat_settlement_communication
# (the two renamed modules are merged into their replacement by its
# `pre_init_hook`, via `openupgrade.update_module_names(merge_modules=True)`).
# The replacement module converts the old data in its init hooks only if
# `openupgrade.is_module_installed()` is true for the old module, which excludes
# the 'to remove' state: marking the old module 'to remove' here (STEP 3, first
# part of the loading) would make the replacement (installed in the second
# part) silently skip the data conversion.
MODULES_REMOVED_BY_REPLACEMENT = {
    "account_vat_period_end_statement",
    "l10n_it_declaration_of_intent",
    "l10n_it_fatturapa",
    "l10n_it_fatturapa_pec",
    "l10n_it_vat_statement_communication",
}


def _mark_dependencies_to_install(cr):
    """Mark as 'to install' the uninstalled dependencies (direct and indirect)
    of every module that is 'to install'."""
    openupgrade.logged_query(
        cr,
        """
        WITH RECURSIVE deps (name) AS (
            SELECT name FROM ir_module_module WHERE state = 'to install'
          UNION
            SELECT d.name
            FROM ir_module_module_dependency d
            JOIN ir_module_module m ON m.id = d.module_id
            JOIN deps ON deps.name = m.name
        )
        UPDATE ir_module_module m
        SET state = 'to install'
        FROM deps
        WHERE m.name = deps.name
          AND m.state = 'uninstalled'
        """,
    )


def _force_install_with_dependencies(cr, module):
    """Mark `module` and all its uninstalled dependencies as 'to install'.

    Flagging only the module itself is not enough: a module whose
    dependencies are not installed nor marked for installation is skipped by
    the loading graph ("Unmet dependencies") and stays 'to install'.
    The auto-installable modules triggered by the new ones are marked too,
    with their own dependencies, as `ir.module.module.button_install()` would
    do; unlike `button_install()`, a module depending on one of the old
    modules retired by this migration is never auto-installed.
    Same algorithm as `_force_install_module` in odoo/upgrade-util.
    """
    openupgrade.logged_query(
        cr,
        """
        UPDATE ir_module_module
        SET state = 'to install'
        WHERE name = %s
          AND state = 'uninstalled'
        """,
        (module,),
    )
    _mark_dependencies_to_install(cr)
    retired_modules = sorted(
        set(OLD_MODULES) | set(OLD_MODULES_TO_REMOVE) | MODULES_REMOVED_BY_REPLACEMENT
    )
    # Same conditions as `must_install()` in `button_install()`: every
    # auto-install-required dependency is installed or being installed, at
    # least one of them is being installed, and the module is not restricted
    # to countries other than the companies' ones.
    while True:
        marked = openupgrade.logged_query(
            cr,
            """
            UPDATE ir_module_module m
            SET state = 'to install'
            WHERE m.state = 'uninstalled'
              AND m.auto_install
              AND NOT EXISTS (
                  SELECT 1
                  FROM ir_module_module_dependency d
                  JOIN ir_module_module dm ON dm.name = d.name
                  WHERE d.module_id = m.id
                    AND d.auto_install_required
                    AND dm.state NOT IN ('installed', 'to install', 'to upgrade')
              )
              AND EXISTS (
                  SELECT 1
                  FROM ir_module_module_dependency d
                  JOIN ir_module_module dm ON dm.name = d.name
                  WHERE d.module_id = m.id
                    AND d.auto_install_required
                    AND dm.state = 'to install'
              )
              AND NOT EXISTS (
                  SELECT 1
                  FROM ir_module_module_dependency d
                  WHERE d.module_id = m.id
                    AND d.name = ANY(%s)
              )
              AND (
                  NOT EXISTS (
                      SELECT 1 FROM module_country mc WHERE mc.module_id = m.id
                  )
                  OR EXISTS (
                      SELECT 1
                      FROM module_country mc
                      -- res.company.country_id is a non-stored related field
                      JOIN res_partner p ON p.country_id = mc.country_id
                      JOIN res_company c ON c.partner_id = p.id
                      WHERE mc.module_id = m.id
                  )
              )
            """,
            (retired_modules,),
        )
        if not marked:
            break
        _mark_dependencies_to_install(cr)


def rename_fields(env, table, field_updates, condition=None):
    """Generic function to rename fields."""
    set_clauses = sql.SQL(", ").join(
        sql.SQL("{} = {}").format(sql.Identifier(target), sql.Identifier(source))
        for target, source in field_updates.items()
    )
    query = sql.SQL("""
        UPDATE {table}
        SET {set_clauses}
    """).format(table=sql.Identifier(table), set_clauses=set_clauses)
    if condition:
        query += sql.SQL(" WHERE {} ").format(sql.SQL(condition))
    openupgrade.logged_query(env.cr, query)


def update_table(env, target_table, source_table, field_updates, condition):
    """Generic function to update fields in a table based on a join."""
    set_clauses = sql.SQL(", ").join(
        sql.SQL("{} = {}.{}").format(
            sql.Identifier(target), sql.Identifier(source_table), sql.Identifier(source)
        )
        for target, source in field_updates.items()
    )
    query = sql.SQL("""
        UPDATE {target_table}
        SET {set_clauses}
        FROM {source_table}
    """).format(
        target_table=sql.Identifier(target_table),
        set_clauses=set_clauses,
        source_table=sql.Identifier(source_table),
    )
    if condition:
        query += sql.SQL(" WHERE {} ").format(sql.SQL(condition))
    openupgrade.logged_query(env.cr, query)


def add_field_if_not_exists(env, table, field_name, field_type, module):
    """Helper function to add fields if they do not exist."""
    if not openupgrade.column_exists(env.cr, table, field_name):
        sql_type_mapping = {
            "binary": "bytea",
            "boolean": "bool",
            "char": "varchar",
            "date": "date",
            "datetime": "timestamp",
            "float": "numeric",
            "html": "text",
            "integer": "int4",
            "many2many": False,
            "many2one": "int4",
            "many2one_reference": "int4",
            "monetary": "numeric",
            "one2many": False,
            "reference": "varchar",
            "selection": "varchar",
            "text": "text",
            "serialized": "text",
        }
        openupgrade.add_fields(
            env,
            [
                (
                    field_name,
                    table.replace("_", "."),
                    table,
                    field_type,
                    sql_type_mapping[field_type],
                    module,
                )
            ],
        )


def _l10n_it_account_tax_kind_migration(env):
    table = "account_tax"
    add_field_if_not_exists(env, table, "l10n_it_law_reference", "char", "l10n_it")
    rename_fields(env, table, {"l10n_it_law_reference": "law_reference"})

    add_field_if_not_exists(env, table, "l10n_it_exempt_reason", "char", "l10n_it")
    condition = "account_tax.kind_id = account_tax_kind.id"
    condition += " AND account_tax.kind_id IS NOT NULL"
    update_table(
        env, table, "account_tax_kind", {"l10n_it_exempt_reason": "code"}, condition
    )


def _account_vat_period_end_statement_migration(env):
    """
    Install "l10n_it_account_vat_period_end_settlement", which replaces
    the old account_vat_period_end_statement module and merges it.
    """
    _force_install_with_dependencies(
        env.cr, "l10n_it_account_vat_period_end_settlement"
    )


def _l10n_it_vat_statement_communication_migration(env):
    """
    Install "l10n_it_vat_settlement_communication", which replaces
    the old l10n_it_vat_statement_communication module and merges it.
    """
    _force_install_with_dependencies(env.cr, "l10n_it_vat_settlement_communication")


def _l10n_it_declaration_of_intent_migration(env):
    """
    Install "l10n_it_edi_doi_extension" which replaces the old
    l10n_it_declaration_of_intent module.
    """
    _force_install_with_dependencies(env.cr, "l10n_it_edi_doi_extension")


def _l10n_it_fatturapa_migration(env):
    # Remove exclusion for installation of "l10n_it_edi"
    query = """
        DELETE
        FROM ir_module_module_exclusion
        WHERE name = 'l10n_it_edi'
    """
    openupgrade.logged_query(env.cr, query)

    # Automatically install `l10n_it_edi_extension` (and its dependencies,
    # e.g. `l10n_it_edi`) because it migrates the data of
    # `l10n_it_fatturapa` and several depending modules,
    # then uninstalls them.
    _force_install_with_dependencies(env.cr, "l10n_it_edi_extension")

    # Drop the views of `l10n_it_fatturapa` and every view inheriting them:
    # some belong to old modules that are not uninstalled in the same step
    # (e.g. `l10n_it_fatturapa_sale`, custom modules), and would make the
    # uninstallation of `l10n_it_fatturapa` fail on the `inherit_id` foreign key.
    # The data conversion does not depend on the views.
    remove_modules_views(env.cr, ["l10n_it_fatturapa"])


def _l10n_it_fatturapa_pec_migration(env):
    """
    Install "l10n_it_edi_pec" which replaces the old
    l10n_it_fatturapa_pec module.
    """
    _force_install_with_dependencies(env.cr, "l10n_it_edi_pec")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for module in OLD_MODULES:
        migration_function = globals().get(f"_{module}_migration")
        if openupgrade.is_module_installed(env.cr, module) and migration_function:
            migration_function(env)
        if module not in MODULES_REMOVED_BY_REPLACEMENT:
            _remove_module(env, module)

    remove_modules_views(cr, OLD_MODULES_TO_REMOVE)
    for module in OLD_MODULES_TO_REMOVE:
        _remove_module(env, module)
