"""
InterSystems SDA3 exporter for Synthea.

SDA3 is the clinical data model HealthShare and HealthConnect use internally:
one ``<Container>`` per patient, holding the patient's demographics and a
pluralised list per streamlet type (``Encounters/Encounter``,
``Problems/Problem`` and so on). A container written here can be loaded with
``HS.SDA3.Container.XMLImportSDAString`` or sent to an ECR/Edge gateway without
passing through the FHIR-to-SDA transforms first.

The data is the same record the FHIR exporter reads; only the shape differs.
Where SDA3 has two homes for one fact, the choice made here is documented at
the point it is made.
"""

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, TYPE_CHECKING
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET

from synthea.export.exporter import PatientExporter
from synthea.export.fhir import (
    _allergy_category,
    _allergy_criticality,
    _device_identifier,
    _device_udi,
    _goal_uuid,
    _note_uuid,
    _stable_uuid,
)
from synthea.export.terminology import system_uri, ucum_code

if TYPE_CHECKING:
    from synthea.world.person import Person
    from synthea.helpers.config import Config


#: Code system URIs mapped to the SDACodingStandard HealthShare registers them
#: under. The modules name systems loosely ('SNOMED-CT', 'RxNorm'), so lookup
#: goes through `system_uri` first and every spelling lands on one standard.
_STANDARDS = {
    'http://snomed.info/sct': 'SCT',
    'http://loinc.org': 'LN',
    'http://www.nlm.nih.gov/research/umls/rxnorm': 'RXNORM',
    'http://hl7.org/fhir/sid/cvx': 'CVX',
    'http://www.ama-assn.org/go/cpt': 'C4',
    'http://hl7.org/fhir/sid/icd-10-cm': 'I10',
    'http://hl7.org/fhir/sid/icd-10': 'I10',
    'http://hl7.org/fhir/sid/icd-9-cm': 'I9',
    'http://hl7.org/fhir/sid/ndc': 'NDC',
    'http://dicom.nema.org/resources/ontology/DCM': 'DCM',
    'http://unitsofmeasure.org': 'UCUM',
}

RACE_STANDARD = 'Race & Ethnicity - CDC'

#: The SendingFacility when the configuration names none.
DEFAULT_FACILITY = 'SYNTHEA_Edge1'

#: SDA3 EncounterType is a closed list of single letters. Everything that is
#: not an admission or an emergency visit is outpatient; `EncounterCodedType`
#: carries the precise SNOMED code alongside it.
ENCOUNTER_TYPES = {
    'inpatient': 'I',
    'snf': 'I',
    'emergency': 'E',
}

#: Problem and Diagnosis status, as the SNOMED codes HealthShare's own
#: transforms write.
STATUS_ACTIVE = ('55561003', 'Active')
STATUS_RESOLVED = ('413322009', 'Resolved')

_GENDERS = {'M': 'Male', 'F': 'Female'}

#: Identifier attribute -> SDA3 PatientNumber.NumberType.
_NUMBER_TYPES = [
    ('identifier_ssn', 'SSN'),
    ('identifier_drivers', 'DL'),
    ('identifier_passport', 'PPN'),
]

_RACE_CODES = {
    'white': ('2106-3', 'White'),
    'black': ('2054-5', 'Black or African American'),
    'asian': ('2028-9', 'Asian'),
    'native': ('1002-5', 'American Indian or Alaska Native'),
    'other': ('2131-1', 'Other Race'),
}


def coding_standard(system: Any) -> Optional[str]:
    """The SDACodingStandard for a module's code system name.

    Unknown systems pass through as written: HealthShare accepts any string
    here, and the module's own name is more useful than nothing.
    """
    if not system:
        return None
    return _STANDARDS.get(system_uri(system), str(system))


def sda_time(value) -> Optional[str]:
    """An SDA3 TimeStamp: second precision, UTC, with a trailing ``Z``.

    Simulation times are naive and read as UTC, the same assumption the FHIR
    exporter makes, so the two outputs describe the same instants.
    """
    if value is None:
        return None
    if getattr(value, 'tzinfo', None) is not None:
        value = value.astimezone(timezone.utc).replace(tzinfo=None)
    return value.replace(microsecond=0).isoformat() + 'Z'


def _number(value) -> str:
    """A number as text, without float noise such as ``5.300000000000001``."""
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip('0').rstrip('.')
        return text if text not in ('', '-0') else '0'
    return str(value)


def _text(parent: ET.Element, tag: str, value: Any) -> Optional[ET.Element]:
    """Append ``<tag>value</tag>``, or nothing when there is no value.

    SDA3 treats an empty element as "clear this field" on update, so an absent
    value must be an absent element rather than an empty one.
    """
    if value is None or value == '':
        return None
    element = ET.SubElement(parent, tag)
    if isinstance(value, (bool, int, float)):
        element.text = _number(value)
    else:
        element.text = str(value)
    return element


def _code_parts(raw: Any):
    """(standard, code, description) from a Code, a code dict or a string."""
    if hasattr(raw, 'code'):
        return coding_standard(raw.system), str(raw.code), raw.display or None
    if isinstance(raw, dict):
        code = raw.get('code')
        return (coding_standard(raw.get('system')),
                str(code) if code is not None else None,
                raw.get('display') or None)
    if raw is None:
        return None, None, None
    return None, str(raw), str(raw)


def _code_table(parent: ET.Element, tag: str, raw: Any = None, *,
                code: Any = None, description: Any = None,
                standard: Optional[str] = None) -> Optional[ET.Element]:
    """Append a CodeTableDetail: SDACodingStandard, Code, Description."""
    parsed_standard, parsed_code, parsed_description = _code_parts(raw)
    code = code if code is not None else parsed_code
    description = description if description is not None else parsed_description
    standard = standard if standard is not None else parsed_standard
    if not code and not description:
        return None

    element = ET.SubElement(parent, tag)
    _text(element, 'SDACodingStandard', standard)
    _text(element, 'Code', code if code else description)
    _text(element, 'Description', description)
    return element


def _list(parent: ET.Element, tag: str) -> ET.Element:
    return ET.SubElement(parent, tag)


def _prune_empty_lists(container: ET.Element) -> None:
    """Drop list wrappers nothing was added to.

    An empty ``<Allergies/>`` is harmless on first load but reads as "this
    patient has no allergies" to anyone looking at the file, which is a claim
    the record never made.
    """
    for child in list(container):
        if len(child) == 0 and not (child.text or '').strip():
            container.remove(child)


def _first(codes: Iterable[Any]) -> Any:
    for code in codes or []:
        return code
    return None


def _description(entry) -> Optional[str]:
    code = _first(entry.codes)
    if code is not None and getattr(code, 'display', None):
        return code.display
    return getattr(entry, 'name', None)


class SDA3Exporter(PatientExporter):
    """Write one SDA3 ``<Container>`` XML file per patient."""

    def __init__(self, config: 'Config', base_dir: Path, locale=None,
                 sending_facility: Optional[str] = None):
        self.config = config
        self.base_dir = base_dir
        self.output_dir = base_dir / 'sda3'
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.locale = locale

        # The sending facility is part of HealthShare's internal MRN
        # (facility^assigning organisation^number), so every container this
        # exporter writes shares it and the MRN's Organization matches it.
        # Passing one explicitly lets a caller feed several facilities from
        # one configuration, one exporter per facility.
        self.sending_facility = str(
            sending_facility
            or config.get('exporter.sda3.sending_facility', DEFAULT_FACILITY)
            or DEFAULT_FACILITY)
        self.pretty = config.get_bool('exporter.pretty_print', True)

    def export(self, person: 'Person', time: int) -> Optional[str]:
        if not hasattr(person, 'record') or not person.record:
            return None

        container = self.create_container(person)
        return self._write(container, self._filename(person))

    def _filename(self, person: 'Person') -> str:
        if self.config.get_bool('exporter.use_uuid_filenames', False):
            return f"{person.id}.xml"

        first_name = person.attributes.get('first_name', 'Unknown')
        last_name = person.attributes.get('last_name', 'Person')
        return f"{first_name}_{last_name}_{person.id[:8]}.xml"

    def _write(self, container: ET.Element, filename: str) -> str:
        if self.pretty:
            ET.indent(container, space='  ')
        filepath = self.output_dir / filename
        ET.ElementTree(container).write(
            filepath, encoding='utf-8', xml_declaration=True)
        return str(filepath)

    def to_string(self, person: 'Person') -> str:
        """The container as a string, for callers that post it somewhere."""
        container = self.create_container(person)
        if self.pretty:
            ET.indent(container, space='  ')
        return ET.tostring(container, encoding='unicode')

    # ------------------------------------------------------------------
    # Container
    # ------------------------------------------------------------------

    def create_container(self, person: 'Person') -> ET.Element:
        """Build the whole container for one patient."""
        record = person.record
        container = ET.Element('Container')
        _text(container, 'SendingFacility', self.sending_facility)
        self._patient(container, person)

        # Encounters first: every other streamlet refers to one by
        # EncounterNumber, and HealthShare rejects a reference to an
        # encounter it has not seen.
        encounters = _list(container, 'Encounters')
        for encounter in record.encounters:
            self._encounter(encounters, encounter)

        allergies = _list(container, 'Allergies')
        for allergy in getattr(record, 'allergies', []):
            self._allergy(allergies, allergy)

        diagnoses = _list(container, 'Diagnoses')
        problems = _list(container, 'Problems')
        for condition in record.conditions:
            self._problem(problems, condition)
            if condition.encounter is not None:
                self._diagnosis(diagnoses, condition)

        observations = _list(container, 'Observations')
        lab_orders = _list(container, 'LabOrders')
        self._results(observations, lab_orders, record)

        procedures = _list(container, 'Procedures')
        for procedure in record.procedures:
            self._procedure(procedures, procedure)

        documents = _list(container, 'Documents')
        self._documents(documents, record)

        rad_orders = _list(container, 'RadOrders')
        for study in getattr(record, 'imaging_studies', []):
            self._rad_order(rad_orders, study)

        medications = _list(container, 'Medications')
        for medication in record.medications:
            self._medication(medications, medication)

        vaccinations = _list(container, 'Vaccinations')
        for immunization in getattr(record, 'immunizations', []):
            self._vaccination(vaccinations, immunization)

        enrollments = _list(container, 'MemberEnrollments')
        self._enrollments(enrollments, person)

        device_items = _list(container, 'DeviceItems')
        for device in getattr(record, 'devices', []):
            self._device_item(device_items, device)

        careplans = _list(container, 'CarePlans')
        goals = _list(container, 'Goals')
        for careplan in getattr(record, 'careplans', []):
            self._careplan(careplans, goals, careplan)

        _prune_empty_lists(container)
        return container

    # ------------------------------------------------------------------
    # Shared pieces
    # ------------------------------------------------------------------

    def _address(self, parent: ET.Element, street, city, state, zip_code,
                 tag: str = 'Address') -> ET.Element:
        address = ET.SubElement(parent, tag)
        _text(address, 'Street', street)
        _code_table(address, 'City', code=city, description=city)
        _code_table(address, 'State', code=state, description=state)
        _code_table(address, 'Zip', code=zip_code, description=zip_code)
        country = getattr(self.locale, 'country_code', None) or 'US'
        _code_table(address, 'Country', code=country, description=country)
        return address

    def _care_provider(self, parent: ET.Element, tag: str,
                       clinician) -> Optional[ET.Element]:
        """A clinician as a CareProvider code table.

        The NPI is the code when there is one, since that is what another
        system would match on; the generator's own id otherwise.
        """
        if clinician is None:
            return None
        first = getattr(clinician, 'first_name', None)
        last = getattr(clinician, 'last_name', None)
        description = ', '.join(part for part in (last, first) if part) or None

        element = ET.SubElement(parent, tag)
        _text(element, 'Code', getattr(clinician, 'npi', None) or clinician.id)
        _text(element, 'Description', description)
        if first or last:
            name = ET.SubElement(element, 'Name')
            _text(name, 'FamilyName', last)
            _text(name, 'GivenName', first)
        specialty = getattr(clinician, 'specialty', None)
        if specialty:
            _code_table(element, 'CareProviderType', code=specialty,
                        description=specialty)
        return element

    def _facility(self, parent: ET.Element, tag: str,
                  provider) -> Optional[ET.Element]:
        """A provider as a HealthCareFacility, with its Organization."""
        if provider is None:
            return None
        element = ET.SubElement(parent, tag)
        _text(element, 'Code', provider.id)
        _text(element, 'Description', provider.name)
        organization = ET.SubElement(element, 'Organization')
        _text(organization, 'Code', provider.id)
        _text(organization, 'Description', provider.name)
        if provider.city or provider.address:
            self._address(element, provider.address, provider.city,
                          provider.state, provider.zip_code)
        if getattr(provider, 'phone', None):
            contact = ET.SubElement(element, 'ContactInfo')
            _text(contact, 'WorkPhoneNumber', provider.phone)
        return element

    @staticmethod
    def _link(element: ET.Element, entry, encounter=None) -> None:
        """ExternalId and EncounterNumber: the two keys HealthShare matches on."""
        _text(element, 'ExternalId', entry.id)
        encounter = encounter if encounter is not None else getattr(entry, 'encounter', None)
        if encounter is not None:
            _text(element, 'EncounterNumber', encounter.id)

    @staticmethod
    def _clinician_of(entry):
        encounter = getattr(entry, 'encounter', None)
        return getattr(encounter, 'clinician', None) if encounter else None

    @staticmethod
    def _provider_of(entry):
        encounter = getattr(entry, 'encounter', None)
        return getattr(encounter, 'provider', None) if encounter else None

    # ------------------------------------------------------------------
    # Patient
    # ------------------------------------------------------------------

    def _patient(self, container: ET.Element, person: 'Person') -> None:
        attributes = person.attributes
        patient = ET.SubElement(container, 'Patient')

        name = ET.SubElement(patient, 'Name')
        _text(name, 'FamilyName', attributes.get('last_name') or 'Unknown')
        _text(name, 'GivenName', attributes.get('first_name') or 'Unknown')
        _text(name, 'NamePrefix', attributes.get('name_prefix'))

        maiden = attributes.get('maiden_name')
        if maiden:
            aliases = ET.SubElement(patient, 'Aliases')
            alias = ET.SubElement(aliases, 'Name')
            _text(alias, 'FamilyName', maiden)
            _text(alias, 'GivenName', attributes.get('first_name'))
            _text(alias, 'Type', 'Maiden')

        language = attributes.get('language_code')
        if language:
            _code_table(patient, 'PrimaryLanguage', code=language.get('code'),
                        description=language.get('display'))

        marital = attributes.get('marital_status')
        if marital:
            _code_table(patient, 'MaritalStatus', code=marital.get('code'),
                        description=marital.get('display'))

        gender = person.gender if person.gender in _GENDERS else 'U'
        _code_table(patient, 'Gender', code=gender,
                    description=_GENDERS.get(gender, 'Unknown'))

        # Race is only recorded where the locale records it. Emitting OMB
        # categories for a country that does not collect them would be
        # inventing data, which is the rule the FHIR exporter follows too.
        records_race = True
        if self.locale is not None:
            records_race = bool(getattr(self.locale, 'race_categories', ()))
        if records_race:
            race_code, race_display = _RACE_CODES.get(person.race, _RACE_CODES['other'])
            _code_table(patient, 'Race', code=race_code, description=race_display,
                        standard=RACE_STANDARD)
            if person.ethnicity == 'non_hispanic':
                ethnicity = ('2186-5', 'Not Hispanic or Latino')
            else:
                ethnicity = ('2135-2', 'Hispanic or Latino')
            _code_table(patient, 'EthnicGroup', code=ethnicity[0],
                        description=ethnicity[1], standard=RACE_STANDARD)

        _text(patient, 'BirthTime', sda_time(person.birth_date))
        dead = not person.alive and person.death_date is not None
        _text(patient, 'IsDead', dead)
        if dead:
            _text(patient, 'DeathTime', sda_time(person.death_date))

        self._patient_numbers(patient, person)

        addresses = ET.SubElement(patient, 'Addresses')
        self._address(addresses, attributes.get('address'),
                      attributes.get('city'), attributes.get('state'),
                      attributes.get('zip_code'))

        phone = attributes.get('telephone')
        email = attributes.get('email')
        if phone or email:
            contact = ET.SubElement(patient, 'ContactInfo')
            _text(contact, 'HomePhoneNumber', phone)
            _text(contact, 'EmailAddress', email)

    def _patient_numbers(self, patient: ET.Element, person: 'Person') -> None:
        """The MRN first, then every other identifier the patient carries.

        HealthShare builds its internal MRN from the first MRN-typed number,
        so it must be present and assigned by the sending facility. The
        generator's id is the fallback, which keeps the container matchable
        to the run that produced it.
        """
        numbers = ET.SubElement(patient, 'PatientNumbers')

        mrn = ET.SubElement(numbers, 'PatientNumber')
        _text(mrn, 'Number', person.attributes.get('identifier_mrn') or person.id)
        organization = ET.SubElement(mrn, 'Organization')
        _text(organization, 'Code', self.sending_facility)
        _text(mrn, 'NumberType', 'MRN')

        for attribute, number_type in _NUMBER_TYPES:
            value = person.attributes.get(attribute)
            if not value:
                continue
            number = ET.SubElement(numbers, 'PatientNumber')
            _text(number, 'Number', value)
            _text(number, 'NumberType', number_type)

    # ------------------------------------------------------------------
    # Encounters and coverage
    # ------------------------------------------------------------------

    def _encounter(self, parent: ET.Element, encounter) -> None:
        element = ET.SubElement(parent, 'Encounter')
        _text(element, 'ExternalId', encounter.id)
        _text(element, 'EncounterNumber', encounter.id)
        _text(element, 'EncounterType',
              ENCOUNTER_TYPES.get(encounter.encounter_class.value, 'O'))
        _code_table(element, 'EncounterCodedType', _first(encounter.codes))
        _text(element, 'VisitDescription', _description(encounter))
        _text(element, 'FromTime', sda_time(encounter.time))
        _text(element, 'ToTime', sda_time(encounter.end_time))
        _text(element, 'EndTime', sda_time(encounter.end_time))

        if encounter.reason:
            _code_table(element, 'AdmitReason', encounter.reason)

        self._facility(element, 'HealthCareFacility', encounter.provider)

        if encounter.clinician is not None:
            clinicians = ET.SubElement(element, 'AttendingClinicians')
            self._care_provider(clinicians, 'CareProvider', encounter.clinician)

        self._health_fund(element, getattr(encounter, 'coverage', None))

    def _health_fund(self, encounter_element: ET.Element, coverage) -> None:
        """The plan that paid for this visit, when there was one."""
        plan = getattr(coverage, 'plan', None)
        if plan is None or getattr(coverage, 'kind', None) == 'none':
            return
        funds = ET.SubElement(encounter_element, 'HealthFunds')
        self._health_fund_body(ET.SubElement(funds, 'HealthFund'), coverage)

    @staticmethod
    def _health_fund_body(element: ET.Element, coverage) -> None:
        plan = coverage.plan
        payer = getattr(plan, 'payer', None)
        if payer is not None:
            _code_table(element, 'HealthFund', code=payer.id,
                        description=payer.name)
        _code_table(element, 'HealthFundPlan', code=plan.id,
                    description=plan.name)
        _text(element, 'PlanType', getattr(coverage, 'kind', None))
        _text(element, 'FromTime', sda_time(coverage.start))
        _text(element, 'ToTime', sda_time(coverage.end))

    def _enrollments(self, parent: ET.Element, person: 'Person') -> None:
        """Coverage history as MemberEnrollments, one per coverage period."""
        history = person.attributes.get('coverage_history') or []
        for index, coverage in enumerate(history):
            if getattr(coverage, 'plan', None) is None:
                continue
            element = ET.SubElement(parent, 'MemberEnrollment')
            identifier = _stable_uuid('coverage', f"{person.id}:{index}")
            _text(element, 'ExternalId', identifier)
            _text(element, 'MemberEnrollmentNumber', identifier)
            _text(element, 'FromTime', sda_time(coverage.start))
            _text(element, 'ToTime', sda_time(coverage.end))
            kind = getattr(coverage, 'kind', None)
            if kind:
                _code_table(element, 'CoverageType', code=kind,
                            description=kind.title())
            self._health_fund_body(ET.SubElement(element, 'HealthFund'), coverage)

    # ------------------------------------------------------------------
    # Conditions and allergies
    # ------------------------------------------------------------------

    @staticmethod
    def _condition_status(element: ET.Element, condition) -> None:
        code, description = STATUS_RESOLVED if condition.end_time else STATUS_ACTIVE
        _code_table(element, 'Status', code=code, description=description,
                    standard='SNOMED CT')

    def _problem(self, parent: ET.Element, condition) -> None:
        """Every condition goes on the problem list."""
        element = ET.SubElement(parent, 'Problem')
        self._link(element, condition)
        _code_table(element, 'Problem', _first(condition.codes))
        _text(element, 'FromTime', sda_time(condition.time))
        _text(element, 'ToTime', sda_time(condition.end_time))
        self._condition_status(element, condition)
        self._care_provider(element, 'Clinician', self._clinician_of(condition))

    def _diagnosis(self, parent: ET.Element, condition) -> None:
        """A condition diagnosed at a visit is also that visit's diagnosis.

        SDA3 keeps the problem list and encounter diagnoses apart, and a
        HealthShare viewer shows them in different places. A condition linked
        to an encounter belongs in both, as it does in a real feed.
        """
        element = ET.SubElement(parent, 'Diagnosis')
        self._link(element, condition)
        _code_table(element, 'Diagnosis', _first(condition.codes))
        _text(element, 'OnsetTime', sda_time(condition.time))
        _text(element, 'IdentificationTime', sda_time(condition.encounter.time))
        _text(element, 'FromTime', sda_time(condition.time))
        _text(element, 'ToTime', sda_time(condition.end_time))
        self._condition_status(element, condition)
        self._care_provider(element, 'DiagnosingClinician',
                            self._clinician_of(condition))

    def _allergy(self, parent: ET.Element, allergy) -> None:
        element = ET.SubElement(parent, 'Allergy')
        self._link(element, allergy)
        _code_table(element, 'Allergy', _first(allergy.codes))
        category = _allergy_category(allergy)
        _code_table(element, 'AllergyCategory', code=category,
                    description=category.title())
        if allergy.reactions:
            # SDA3 holds a single reaction; the first is the one the module
            # names as the primary.
            _code_table(element, 'Reaction', allergy.reactions[0])
        if allergy.severity:
            _code_table(element, 'Severity', code=allergy.severity,
                        description=str(allergy.severity).title())
            criticality = _allergy_criticality(allergy.severity)
            _code_table(element, 'Criticality', code=criticality,
                        description=criticality)
        _text(element, 'Status', 'I' if allergy.end_time else 'A')
        _text(element, 'DiscoveryTime', sda_time(allergy.time))
        _text(element, 'FromTime', sda_time(allergy.time))
        _text(element, 'ToTime', sda_time(allergy.end_time))
        _text(element, 'InactiveTime', sda_time(allergy.end_time))
        self._care_provider(element, 'Clinician', self._clinician_of(allergy))

    # ------------------------------------------------------------------
    # Observations and lab results
    # ------------------------------------------------------------------

    def _results(self, observations: ET.Element, lab_orders: ET.Element,
                 record) -> None:
        """Split the record's observations between Observations and LabOrders.

        SDA3 has no DiagnosticReport: a lab result is a LabOrder whose Result
        holds one LabResultItem per test. A report with any laboratory result
        becomes one order. A report of surveys or vitals has no order to
        belong to, so its members become Observations grouped by GroupId.
        Laboratory observations outside any report get an order each, so that
        no lab value ends up somewhere a lab viewer will not look.
        """
        reported = set()
        for report in getattr(record, 'reports', []):
            members = list(report.observations or [])
            reported.update(id(observation) for observation in members)
            if any(observation.category == 'laboratory' for observation in members):
                self._lab_order(lab_orders, report, members)
            else:
                for observation in members:
                    self._observation(observations, observation, group=report.id)

        for observation in record.observations:
            if id(observation) in reported:
                continue
            if observation.category == 'laboratory':
                self._lab_order(lab_orders, observation, [observation])
            else:
                self._observation(observations, observation)

    def _observation(self, parent: ET.Element, observation,
                     group: Optional[str] = None) -> None:
        """One Observation, or one per component for a panel.

        SDA3 Observations are single-valued. A blood pressure becomes a
        systolic and a diastolic sharing the panel's id as GroupId.
        """
        components = getattr(observation, 'components', None) or []
        if observation.value is not None or not components:
            self._observation_element(
                parent, observation, observation.id, _first(observation.codes),
                observation.value, observation.unit, group)

        for index, (code, value, unit) in enumerate(components):
            self._observation_element(
                parent, observation, f"{observation.id}-{index}", code, value,
                unit, group or observation.id)

    def _observation_element(self, parent: ET.Element, observation,
                             identifier: str, code, value, unit,
                             group: Optional[str]) -> None:
        element = ET.SubElement(parent, 'Observation')
        _text(element, 'ExternalId', identifier)
        if observation.encounter is not None:
            _text(element, 'EncounterNumber', observation.encounter.id)
        _text(element, 'ObservationTime', sda_time(observation.time))

        code_element = _code_table(element, 'ObservationCode', code)
        if code_element is not None and unit:
            _code_table(code_element, 'ObservationValueUnits',
                        code=ucum_code(unit) or unit, description=unit)

        self._value(element, value, text_tag='ObservationValue',
                    coded_tag='ObservationCodedValue')
        _text(element, 'GroupId', group)
        if observation.category:
            categories = ET.SubElement(element, 'Categories')
            _code_table(categories, 'Category', code=observation.category,
                        description=observation.category)
        self._interpretation(element, observation)
        self._reference_range(element, observation)
        self._care_provider(element, 'Clinician', self._clinician_of(observation))

    @staticmethod
    def _value(element: ET.Element, value, text_tag: str, coded_tag: str) -> None:
        """A value as SDA3 text, with a coded value's code alongside."""
        if value is None:
            return
        if isinstance(value, dict) or hasattr(value, 'system'):
            _code_table(element, coded_tag, value)
            _, code, display = _code_parts(value)
            _text(element, text_tag, display or code)
        else:
            _text(element, text_tag, value)

    @staticmethod
    def _interpretation(element: ET.Element, observation) -> None:
        flag = getattr(observation, 'interpretation', None)
        if flag:
            code, display = flag
            _code_table(element, 'InterpretationCode', code=code,
                        description=display)

    @staticmethod
    def _reference_range(element: ET.Element, observation) -> None:
        interval = getattr(observation, 'reference_range', None)
        if not interval or (interval.get('low') is None
                            and interval.get('high') is None):
            return
        unit = interval.get('unit') or observation.unit
        reference = ET.SubElement(element, 'ReferenceRange')
        for bound in ('low', 'high'):
            if interval.get(bound) is None:
                continue
            quantity = ET.SubElement(reference, bound.title())
            _text(quantity, 'Value', interval[bound])
            if unit:
                _code_table(quantity, 'UnitOfMeasure',
                            code=ucum_code(unit) or unit, description=unit)

    def _lab_order(self, parent: ET.Element, source, members: List[Any]) -> None:
        """A LabOrder whose Result holds every member as a LabResultItem.

        `source` is the report when there is one, otherwise the lone
        observation; either way it supplies the order's code and time.
        """
        element = ET.SubElement(parent, 'LabOrder')
        self._link(element, source)
        _text(element, 'PlacerId', source.id)
        _text(element, 'FillerId', source.id)
        _code_table(element, 'OrderItem', _first(source.codes))
        _text(element, 'Status', 'E')
        _text(element, 'EnteredOn', sda_time(source.time))
        _text(element, 'FromTime', sda_time(source.time))
        _text(element, 'SpecimenCollectedTime', sda_time(source.time))
        self._care_provider(element, 'OrderedBy', self._clinician_of(source))
        self._facility(element, 'EnteringOrganization', self._provider_of(source))

        result = ET.SubElement(element, 'Result')
        _text(result, 'ResultTime', sda_time(source.time))
        _text(result, 'ResultType', 'AT')
        items = ET.SubElement(result, 'ResultItems')
        for observation in members:
            components = getattr(observation, 'components', None) or []
            if observation.value is not None or not components:
                self._result_item(items, observation, _first(observation.codes),
                                  observation.value, observation.unit)
            for code, value, unit in components:
                self._result_item(items, observation, code, value, unit,
                                  with_range=False)

    def _result_item(self, parent: ET.Element, observation, code, value, unit,
                     with_range: bool = True) -> None:
        element = ET.SubElement(parent, 'LabResultItem')
        _code_table(element, 'TestItemCode', code)
        self._value(element, value, text_tag='ResultValue',
                    coded_tag='ResultCodedValue')
        _text(element, 'ResultValueUnits', unit)
        _text(element, 'ObservationTime', sda_time(observation.time))
        if not with_range:
            return

        # ResultNormalRange is the display string lab viewers show, and
        # ResultInterpretation the HL7 v2 abnormal flag; the coded forms sit
        # alongside for anything that wants structure.
        interval = getattr(observation, 'reference_range', None) or {}
        low, high = interval.get('low'), interval.get('high')
        if low is not None and high is not None:
            _text(element, 'ResultNormalRange', f"{_number(low)}-{_number(high)}")
        elif low is not None:
            _text(element, 'ResultNormalRange', f">{_number(low)}")
        elif high is not None:
            _text(element, 'ResultNormalRange', f"<{_number(high)}")

        flag = getattr(observation, 'interpretation', None)
        if flag:
            _text(element, 'ResultInterpretation', flag[0])
        self._interpretation(element, observation)
        self._reference_range(element, observation)

    # ------------------------------------------------------------------
    # Procedures, imaging, notes
    # ------------------------------------------------------------------

    def _procedure(self, parent: ET.Element, procedure) -> None:
        element = ET.SubElement(parent, 'Procedure')
        self._link(element, procedure)
        _code_table(element, 'Procedure', _first(procedure.codes))
        _text(element, 'ProcedureTime', sda_time(procedure.time))
        _text(element, 'FromTime', sda_time(procedure.time))
        if procedure.duration:
            end = procedure.time + timedelta(minutes=float(procedure.duration))
            _text(element, 'ToTime', sda_time(end))
        _code_table(element, 'Status', code='completed', description='Completed')
        if procedure.reason:
            _code_table(element, 'ProcedureReason', procedure.reason)
        self._care_provider(element, 'Clinician', self._clinician_of(procedure))
        self._facility(element, 'Location', self._provider_of(procedure))

    def _rad_order(self, parent: ET.Element, study) -> None:
        """An imaging study as the RadOrder that produced it.

        The study UID is the filler's identifier for the order, which is what
        a PACS would give back.
        """
        element = ET.SubElement(parent, 'RadOrder')
        self._link(element, study)
        _text(element, 'PlacerId', study.id)
        _text(element, 'FillerId', getattr(study, 'dicom_uid', None))
        _code_table(element, 'OrderItem',
                    getattr(study, 'procedure_code', None) or _first(study.codes))
        _text(element, 'Status', 'E')
        _text(element, 'EnteredOn', sda_time(study.time))
        _text(element, 'FromTime', sda_time(study.time))
        self._care_provider(element, 'OrderedBy', self._clinician_of(study))
        self._facility(element, 'EnteringOrganization', self._provider_of(study))

    def _documents(self, parent: ET.Element, record) -> None:
        """Each encounter's clinical note as a Document."""
        from synthea.world.notes import NOTE_ATTRIBUTE, NOTE_CODE, NOTE_DISPLAY

        for encounter in record.encounters:
            text = getattr(encounter, NOTE_ATTRIBUTE, None)
            if not text:
                continue
            identifier = _note_uuid(encounter)
            element = ET.SubElement(parent, 'Document')
            _text(element, 'ExternalId', identifier)
            _text(element, 'EncounterNumber', encounter.id)
            _text(element, 'DocumentNumber', identifier)
            _text(element, 'DocumentName', NOTE_DISPLAY)
            _code_table(element, 'DocumentType', code=NOTE_CODE,
                        description=NOTE_DISPLAY, standard='LN')
            _text(element, 'DocumentTime', sda_time(encounter.time))
            _text(element, 'FileType', 'TXT')
            _text(element, 'NoteText', text)
            self._care_provider(element, 'Clinician', encounter.clinician)

    # ------------------------------------------------------------------
    # Medications and vaccinations
    # ------------------------------------------------------------------

    def _medication(self, parent: ET.Element, medication) -> None:
        """A prescription, or a drug given during the visit.

        Both are SDA3 Medications. One given in the visit is an executed order
        carrying an Administration; a prescription is in progress until it
        ends, and discontinued after.
        """
        element = ET.SubElement(parent, 'Medication')
        self._link(element, medication)
        code = _first(medication.codes)
        _code_table(element, 'OrderItem', code)
        _code_table(element, 'DrugProduct', code)
        _text(element, 'EnteredOn', sda_time(medication.time))
        _text(element, 'FromTime', sda_time(medication.time))
        _text(element, 'ToTime', sda_time(medication.end_time))

        administered = getattr(medication, 'administration', False)
        if administered:
            status = 'E'
        else:
            status = 'D' if medication.end_time else 'IP'
        _text(element, 'Status', status)

        clinician = self._clinician_of(medication)
        self._care_provider(element, 'OrderedBy', clinician)
        self._facility(element, 'EnteringOrganization', self._provider_of(medication))

        reason_entry = getattr(medication, 'reason_entry', None)
        if reason_entry is not None and reason_entry.codes:
            _code_table(element, 'IndicationCoded', _first(reason_entry.codes))
            _text(element, 'Indication', _description(reason_entry))
        elif medication.reason:
            _text(element, 'Indication', medication.reason)

        self._prescription(element, medication)

        if administered:
            administrations = ET.SubElement(element, 'Administrations')
            administration = ET.SubElement(administrations, 'Administration')
            _text(administration, 'ExternalId', f"{medication.id}-admin")
            _text(administration, 'FromTime', sda_time(medication.time))
            _code_table(administration, 'AdministrationStatus',
                        code='completed', description='Completed')
            self._care_provider(administration, 'AdministeringProvider', clinician)

    @staticmethod
    def _prescription(element: ET.Element, medication) -> None:
        """Dose, frequency, refills and instructions from the GMF block."""
        prescription = getattr(medication, 'prescription', None) or {}
        if not prescription:
            return

        _text(element, 'AsNeeded', bool(prescription.get('as_needed')))
        refills = prescription.get('refills')
        if refills is not None:
            _text(element, 'NumberOfRefills', refills)

        dosage = prescription.get('dosage') or {}
        duration = prescription.get('duration') or {}
        instructions = prescription.get('instructions') or []
        if not (dosage or duration or instructions):
            return

        steps = ET.SubElement(element, 'DosageSteps')
        step = ET.SubElement(steps, 'DosageStep')
        # `unit` in the GMF block is the period's unit (days, hours), not the
        # dose's, so it belongs to the frequency and there is no DoseUoM.
        _text(step, 'DoseQuantity', dosage.get('amount'))

        frequency, period = dosage.get('frequency'), dosage.get('period')
        if frequency is not None and period is not None:
            unit = dosage.get('unit') or 'day'
            text = f"{_number(frequency)} per {_number(period)} {unit}"
            _code_table(step, 'Frequency', code=text, description=text)
            _text(step, 'TextInstruction', text)

        if duration.get('quantity') is not None:
            text = f"{_number(duration['quantity'])} {duration.get('unit') or ''}".strip()
            _code_table(step, 'Duration', code=text, description=text)

        if instructions:
            _code_table(step, 'CodedInstruction', instructions[0])
            _text(step, 'PatientInstruction', '; '.join(
                filter(None, (_code_parts(item)[2] for item in instructions))))

    def _vaccination(self, parent: ET.Element, immunization) -> None:
        element = ET.SubElement(parent, 'Vaccination')
        self._link(element, immunization)
        code = _first(immunization.codes)
        _code_table(element, 'OrderItem', code)
        _code_table(element, 'DrugProduct', code)
        _text(element, 'EnteredOn', sda_time(immunization.time))
        _text(element, 'FromTime', sda_time(immunization.time))
        _text(element, 'Status', 'E')
        if getattr(immunization, 'dose_number', None):
            _text(element, 'RefillNumber', immunization.dose_number)

        clinician = self._clinician_of(immunization)
        self._care_provider(element, 'OrderedBy', clinician)

        administrations = ET.SubElement(element, 'Administrations')
        administration = ET.SubElement(administrations, 'Administration')
        _text(administration, 'ExternalId', f"{immunization.id}-admin")
        _text(administration, 'FromTime', sda_time(immunization.time))
        _code_table(administration, 'AdministrationStatus',
                    code='completed', description='Completed')
        self._care_provider(administration, 'AdministeringProvider', clinician)
        self._facility(administration, 'AdministeredAtLocation',
                       self._provider_of(immunization))

    # ------------------------------------------------------------------
    # Devices and care plans
    # ------------------------------------------------------------------

    def _device_item(self, parent: ET.Element, device) -> None:
        element = ET.SubElement(parent, 'DeviceItem')
        self._link(element, device)
        _text(element, 'FromTime', sda_time(device.time))
        _text(element, 'ToTime', sda_time(device.end_time))

        inner = ET.SubElement(element, 'Device')
        _code_table(inner, 'Device', _first(device.codes))
        _text(inner, 'UDIHumanReadable', _device_udi(device))
        _text(inner, 'DistinctIdentifier', _device_identifier(device))
        _text(inner, 'ManufactureDate', sda_time(device.time))
        _text(inner, 'ExpirationDate', sda_time(device.end_time))
        status = 'inactive' if device.end_time else 'active'
        _code_table(inner, 'Status', code=status, description=status.title())

    def _careplan(self, careplans: ET.Element, goals: ET.Element, careplan) -> None:
        """A care plan, with its goals as separate Goal streamlets.

        SDA3 links a plan to its goals by ExternalId, so the ids used here are
        the same stable ones the FHIR exporter gives the Goal resources.
        """
        goal_ids = [_goal_uuid(careplan, index)
                    for index in range(len(careplan.goals or []))]

        element = ET.SubElement(careplans, 'CarePlan')
        self._link(element, careplan)
        _code_table(element, 'Type', _first(careplan.codes))
        _text(element, 'Status', 'I' if careplan.end_time else 'A')
        _text(element, 'FromTime', sda_time(careplan.time))
        _text(element, 'ToTime', sda_time(careplan.end_time))

        if goal_ids:
            ids = ET.SubElement(element, 'GoalIds')
            for goal_id in goal_ids:
                _text(ids, 'GoalIdsItem', goal_id)

        if careplan.activities:
            interventions = ET.SubElement(element, 'Interventions')
            for activity in careplan.activities:
                intervention = ET.SubElement(interventions, 'Intervention')
                _, code, display = _code_parts(activity)
                _text(intervention, 'Description', display or code)
                _text(intervention, 'Status',
                      'completed' if careplan.end_time else 'in-progress')

        status = 'completed' if careplan.end_time else 'active'
        for goal_id, goal in zip(goal_ids, careplan.goals or []):
            goal_element = ET.SubElement(goals, 'Goal')
            _text(goal_element, 'ExternalId', goal_id)
            if careplan.encounter is not None:
                _text(goal_element, 'EncounterNumber', careplan.encounter.id)
            _text(goal_element, 'FromTime', sda_time(careplan.time))
            _text(goal_element, 'ToTime', sda_time(careplan.end_time))
            _, code, display = _code_parts(goal)
            _text(goal_element, 'Description', display or code)
            if hasattr(goal, 'code'):
                _code_table(goal_element, 'DescriptionCoded', goal)
            _code_table(goal_element, 'LifecycleStatus', code=status,
                        description=status.title())
