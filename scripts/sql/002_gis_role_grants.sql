DO $$
DECLARE
  table_name text;
BEGIN
  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sedapal') THEN
    GRANT USAGE ON SCHEMA public TO sedapal;
    FOREACH table_name IN ARRAY ARRAY[
      'anomalies', 'commercial_inspections', 'customer_debts', 'customer_supplies',
      'customer_supplies_territory', 'customer_supply_billing_daily',
      'customer_supply_state_readings', 'customers', 'diameter_catalog',
      'domestic_connections', 'gis_blocks', 'gis_cadastral_lot_geometries',
      'gis_cadastral_lot_units', 'gis_districts', 'gis_geometry_corrections',
      'gis_lots', 'gis_quadrants', 'gis_supply_locations', 'gis_supply_lot_links',
      'meter_contrastations', 'meter_park_snapshots', 'meter_registry', 'meters',
      'network_pipes', 'supervision_code_catalog', 'territory_districts', 'work_orders'
    ] LOOP
      IF to_regclass('public.' || table_name) IS NOT NULL THEN
        EXECUTE format('GRANT SELECT ON TABLE public.%I TO sedapal', table_name);
      END IF;
    END LOOP;
    GRANT INSERT, UPDATE, DELETE ON TABLE public.gis_geometry_corrections TO sedapal;
    GRANT INSERT, UPDATE, DELETE ON TABLE public.gis_supply_locations TO sedapal;
    GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO sedapal;
  END IF;

  IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'sedapalgis_martin') THEN
    GRANT USAGE ON SCHEMA public TO sedapalgis_martin;
    FOREACH table_name IN ARRAY ARRAY[
      'customer_supplies', 'gis_blocks', 'gis_districts', 'gis_lots',
      'gis_quadrants', 'gis_supply_locations', 'network_pipes'
    ] LOOP
      IF to_regclass('public.' || table_name) IS NOT NULL THEN
        EXECUTE format('GRANT SELECT ON TABLE public.%I TO sedapalgis_martin', table_name);
      END IF;
    END LOOP;
  END IF;
END $$;
