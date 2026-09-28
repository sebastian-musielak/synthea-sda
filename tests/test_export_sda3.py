"""Tests for the InterSystems SDA3 exporter.

The properties worth holding onto:

1. Every EncounterNumber resolves to an Encounter in the same container.
   HealthShare logs an error and drops the streamlet otherwise, so a dangling
   reference is lost data, not a cosmetic problem.
2. The first PatientNumber is an MRN assigned by the SendingFacility, because
   that is what HealthShare builds its internal MRN from.
3. Code systems are written as the SDACodingStandard HealthShare registers,
   not the modules' own spelling.
4. Lab results arrive as LabOrders carrying their reference interval; other
   observations are Observations.
5. The same seed produces the same bytes.
"""

import xml.etree.ElementTree as ET
from datetime import datetime

import pytest

from synthea.engine.generator import Generator, GeneratorOptions
from synthea.export.exporter import Exporter
from synthea.export.sda3 import SDA3Exporter, coding_standard, sda_time
from synthea.helpers.config import Config
from synthea.world.health_record import Code, EncounterClass
from synthea.world.person import Person

REFERENCE_DATE = datetime(2020, 1, 1)
VISIT = datetime(2015, 6, 1, 9, 30)


def _config(**overrides):
    config = Config()
    config.load()
    for key, value in overrides.items():
        config.set(key, value)
    return config


@pytest.fixture
def person():
    person = Person(seed=3)
    person.init_health_record()
    person.attributes.update({
        'gender': 'F',
        'birth_date': datetime(1975, 6, 1),
        'first_name': 'Ada',
        'last_name': 'Lovelace',
        'maiden_name': 'Byron',
        'race': 'white',
        'ethnicity': 'non_hispanic',
        'identifier_ssn': '999-12-3456',
        'city': 'Boston',
        'state': 'MA',
        'zip_code': '02115',
    })
    record = person.record
    encounter = record.encounter_start(VISIT, EncounterClass.AMBULATORY)
    encounter.codes = [Code('SNOMED-CT', '185349003', 'Encounter for check up')]

    record.condition_start(VISIT, Code('SNOMED-CT', '73211009', 'Diabetes'))
    onset = record.condition_start(
        datetime(2010, 1, 1), Code('SNOMED-CT', '38341003', 'Hypertension'),
        attach=False)
    record.condition_end(onset, datetime(2012, 1, 1))

    allergy = record.allergy_start(VISIT, Code('RxNorm', '7980', 'Penicillin G'))
    allergy.reactions = ['Hives']
    allergy.severity = 'severe'

    glucose = record.observation(
        VISIT, Code('LOINC', '2339-0', 'Glucose'), 180.0, 'mg/dL')
    glucose.category = 'laboratory'
    glucose.reference_range = {'low': 70, 'high': 99, 'unit': 'mg/dL'}
    glucose.interpretation = ('H', 'High')

    height = record.observation(
        VISIT, Code('LOINC', '8302-2', 'Body Height'), 170.2, 'cm')
    height.category = 'vital-signs'

    pressure = record.observation(
        VISIT, Code('LOINC', '85354-9', 'Blood pressure panel'))
    pressure.category = 'vital-signs'
    pressure.components = [
        (Code('LOINC', '8480-6', 'Systolic'), 120, 'mm[Hg]'),
        (Code('LOINC', '8462-4', 'Diastolic'), 80, 'mm[Hg]'),
    ]

    medication = record.medication_start(
        VISIT, Code('RxNorm', '860975', 'Metformin 500 MG'))
    medication.prescription = {
        'dosage': {'amount': 1, 'frequency': 2, 'period': 1, 'unit': 'days'},
        'refills': 3,
    }

    careplan = record.careplan_start(
        VISIT, Code('SNOMED-CT', '698360004', 'Diabetes self management plan'))
    careplan.activities = [Code('SNOMED-CT', '160670007', 'Diabetic diet')]
    careplan.goals = ['Glucose below 7.0']

    record.immunization(VISIT, Code('CVX', '140', 'Influenza, seasonal'))
    record.encounter_end(encounter, datetime(2015, 6, 1, 10, 0))
    return person


@pytest.fixture
def container(person, tmp_path):
    return SDA3Exporter(_config(), tmp_path).create_container(person)


def test_file_is_written_and_parses(person, tmp_path):
    path = SDA3Exporter(_config(), tmp_path).export(person, 0)

    assert path.endswith('.xml')
    assert '/sda3/' in path
    assert ET.parse(path).getroot().tag == 'Container'


def test_every_encounter_number_resolves(container):
    known = {e.findtext('EncounterNumber') for e in container.iter('Encounter')}
    referenced = [e.text for e in container.iter('EncounterNumber')]

    assert referenced
    assert set(referenced) <= known


def test_mrn_is_first_and_assigned_by_the_sending_facility(person, tmp_path):
    config = _config(**{'exporter.sda3.sending_facility': 'ACME'})
    container = SDA3Exporter(config, tmp_path).create_container(person)

    assert container.findtext('SendingFacility') == 'ACME'
    first = container.find('Patient/PatientNumbers/PatientNumber')
    assert first.findtext('NumberType') == 'MRN'
    assert first.findtext('Organization/Code') == 'ACME'

    types = [n.findtext('NumberType')
             for n in container.iter('PatientNumber')]
    assert 'SSN' in types


def test_default_sending_facility_is_edge1(person, tmp_path):
    container = SDA3Exporter(_config(), tmp_path).create_container(person)
    assert container.findtext('SendingFacility') == 'SYNTHEA_Edge1'


def test_one_configuration_can_feed_several_facilities(person, tmp_path):
    """An explicit facility wins over the configured one, MRN included."""
    config = _config()
    for facility in ('SYNTHEA_Edge2', 'SYNTHEA_Edge5'):
        exporter = SDA3Exporter(config, tmp_path, sending_facility=facility)
        container = exporter.create_container(person)
        assert container.findtext('SendingFacility') == facility
        assert container.findtext(
            'Patient/PatientNumbers/PatientNumber/Organization/Code') == facility


def test_patient_demographics(container):
    patient = container.find('Patient')

    assert patient.findtext('Name/FamilyName') == 'Lovelace'
    assert patient.findtext('Aliases/Name/Type') == 'Maiden'
    assert patient.findtext('Gender/Code') == 'F'
    assert patient.findtext('BirthTime') == '1975-06-01T00:00:00Z'
    assert patient.findtext('IsDead') == 'false'
    assert patient.findtext('Race/Code') == '2106-3'
    assert patient.findtext('Addresses/Address/City/Code') == 'Boston'


def test_race_is_not_invented_for_a_locale_that_does_not_record_it(
        person, tmp_path):
    class NoRace:
        race_categories = ()
        country_code = 'NL'

    container = SDA3Exporter(_config(), tmp_path, NoRace()).create_container(person)

    assert container.find('Patient/Race') is None
    assert container.find('Patient/EthnicGroup') is None
    assert container.findtext('Patient/Addresses/Address/Country/Code') == 'NL'


def test_code_systems_use_healthshare_coding_standards():
    assert coding_standard('SNOMED-CT') == 'SCT'
    assert coding_standard('LOINC') == 'LN'
    assert coding_standard('RxNorm') == 'RXNORM'
    assert coding_standard('CVX') == 'CVX'
    assert coding_standard('http://loinc.org') == 'LN'
    assert coding_standard('SOMETHING-ELSE') == 'SOMETHING-ELSE'


def test_timestamps_are_utc_seconds():
    assert sda_time(datetime(2015, 6, 1, 9, 30, 5, 123456)) == '2015-06-01T09:30:05Z'
    assert sda_time(None) is None


def test_conditions_are_problems_and_visit_conditions_are_diagnoses(container):
    problems = container.findall('Problems/Problem')
    diagnoses = container.findall('Diagnoses/Diagnosis')

    assert {p.findtext('Problem/Code') for p in problems} == {'73211009', '38341003'}
    # Only the one diagnosed at the visit is that visit's diagnosis.
    assert [d.findtext('Diagnosis/Code') for d in diagnoses] == ['73211009']
    assert diagnoses[0].findtext('Diagnosis/SDACodingStandard') == 'SCT'

    statuses = {p.findtext('Problem/Code'): p.findtext('Status/Code')
                for p in problems}
    assert statuses == {'73211009': '55561003', '38341003': '413322009'}


def test_lab_results_are_lab_orders_with_their_interval(container):
    orders = container.findall('LabOrders/LabOrder')
    assert len(orders) == 1

    item = orders[0].find('Result/ResultItems/LabResultItem')
    assert orders[0].findtext('Result/ResultType') == 'AT'
    assert item.findtext('TestItemCode/Code') == '2339-0'
    assert item.findtext('TestItemCode/SDACodingStandard') == 'LN'
    assert item.findtext('ResultValue') == '180'
    assert item.findtext('ResultValueUnits') == 'mg/dL'
    assert item.findtext('ResultNormalRange') == '70-99'
    assert item.findtext('ResultInterpretation') == 'H'
    assert item.findtext('ReferenceRange/High/Value') == '99'


def test_panels_are_split_into_grouped_observations(container):
    observations = container.findall('Observations/Observation')
    codes = {o.findtext('ObservationCode/Code'): o for o in observations}

    assert '2339-0' not in codes
    assert codes['8302-2'].findtext('ObservationValue') == '170.2'
    assert codes['8302-2'].findtext(
        'ObservationCode/ObservationValueUnits/Code') == 'cm'

    systolic, diastolic = codes['8480-6'], codes['8462-4']
    assert systolic.findtext('GroupId') == diastolic.findtext('GroupId')
    assert systolic.findtext('ObservationValue') == '120'


def test_medication_carries_its_prescription(container):
    medication = container.find('Medications/Medication')

    assert medication.findtext('OrderItem/SDACodingStandard') == 'RXNORM'
    assert medication.findtext('Status') == 'IP'
    assert medication.findtext('NumberOfRefills') == '3'
    step = medication.find('DosageSteps/DosageStep')
    assert step.findtext('DoseQuantity') == '1'
    assert step.findtext('Frequency/Code') == '2 per 1 days'


def test_allergy(container):
    allergy = container.find('Allergies/Allergy')

    assert allergy.findtext('Allergy/Code') == '7980'
    assert allergy.findtext('AllergyCategory/Code') == 'medication'
    assert allergy.findtext('Reaction/Description') == 'Hives'
    assert allergy.findtext('Criticality/Code') == 'high'
    assert allergy.findtext('Status') == 'A'


def test_care_plan_goals_are_linked_by_id(container):
    careplan = container.find('CarePlans/CarePlan')
    goal = container.find('Goals/Goal')

    assert careplan.findtext('GoalIds/GoalIdsItem') == goal.findtext('ExternalId')
    assert goal.findtext('Description') == 'Glucose below 7.0'
    assert careplan.findtext(
        'Interventions/Intervention/Description') == 'Diabetic diet'


def test_vaccination(container):
    vaccination = container.find('Vaccinations/Vaccination')

    assert vaccination.findtext('OrderItem/SDACodingStandard') == 'CVX'
    assert vaccination.find('Administrations/Administration') is not None


def test_empty_lists_are_left_out(container):
    assert container.find('DeviceItems') is None
    assert container.find('RadOrders') is None


def test_exporter_is_off_by_default_and_on_when_asked(tmp_path):
    def names(config):
        exporter = Exporter.__new__(Exporter)
        exporter.config = config
        exporter.base_dir = tmp_path
        exporter.locale = None
        exporter.patient_exporters = []
        exporter._init_exporters()
        return [type(e).__name__ for e in exporter.patient_exporters]

    assert 'SDA3Exporter' not in names(_config())
    assert 'SDA3Exporter' in names(_config(**{'exporter.sda3.export': True}))


def _run(output_dir):
    config = _config(**{
        'exporter.baseDirectory': str(output_dir),
        'exporter.fhir.export': False,
        'exporter.sda3.export': True,
    })
    options = GeneratorOptions()
    options.population_size = 2
    options.seed = 11
    options.min_age = 30
    options.max_age = 70
    options.reference_date = REFERENCE_DATE
    Generator(options, config=config).run()
    return sorted((output_dir / 'sda3').glob('*.xml'))


def test_population_output_is_valid_and_reproducible(tmp_path):
    first = _run(tmp_path / 'a')
    second = _run(tmp_path / 'b')

    assert len(first) == 2
    assert [p.name for p in first] == [p.name for p in second]
    for a, b in zip(first, second):
        assert a.read_bytes() == b.read_bytes()

        container = ET.parse(a).getroot()
        known = {e.findtext('EncounterNumber') for e in container.iter('Encounter')}
        assert {e.text for e in container.iter('EncounterNumber')} <= known
