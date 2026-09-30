#!/usr/bin/env python
"""
InterSystems SDA3 facility feeds.

Generates a synthetic population and writes it as SDA3 ``<Container>`` XML,
one feed (directory) per HealthShare Edge gateway. Every container a feed
holds names that feed's facility as its SendingFacility and as the
Organization that assigned the patient's MRN, so a folder can be pointed at an
Edge's SDA3 file inbound as-is.

By default there is one feed, ``SYNTHEA_Edge1``. Up to five can be fed in
parallel (``SYNTHEA_Edge1`` .. ``SYNTHEA_Edge5``), and there are two ways to
decide what each one receives:

``--routing patient`` (default)
    Every patient is registered at exactly one facility, picked by a stable
    hash of the patient id, and that facility sends the whole record. The
    feeds are disjoint: useful for load testing several Edges at once.

``--routing encounter``
    Every healthcare facility the simulation uses belongs to one Edge (again
    by stable hash), and each Edge sends only the encounters that happened at
    its facilities, with everything recorded during them. Demographics go to
    every Edge the patient visited, and each Edge assigns its own MRN, so the
    same person arrives at the Registry from several places under different
    MRNs. That is the case the MPI has to link, and the case a clinical viewer
    has to aggregate.

    History that belongs to no encounter (conditions recorded without a
    visit, for instance) goes to the patient's home Edge: the one that saw
    them most, which also keeps the patient's original MRN.

Generation runs across worker processes. Each patient's seed depends only on
the population seed and the patient's index, so the output is identical
whatever ``--workers`` is set to.

Output::

    <output>/
        SYNTHEA_Edge1/
          Ada_Lovelace_1a2b3c4d.xml
          ...
          manifest.csv       load order, MRN and counts, one row per container
        SYNTHEA_Edge2/
          ...
      feeds_report.json      run settings, per-feed totals and integrity checks

Usage:
    uv run python examples/sda3_facility_feeds.py
    uv run python examples/sda3_facility_feeds.py --facilities 3
    uv run python examples/sda3_facility_feeds.py --facilities 5 --routing encounter -p 200 -w 8
    uv run python examples/sda3_facility_feeds.py --facilities SYNTHEA_Edge2,SYNTHEA_Edge4
"""

import argparse
import concurrent.futures
import copy
import csv
import hashlib
import json
import os
import shutil
import sys
import time
import xml.etree.ElementTree as ET
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from synthea import Generator, GeneratorOptions
from synthea.export.sda3 import SDA3Exporter
from synthea.helpers.config import Config


FACILITY_PREFIX = 'SYNTHEA_Edge'
MAX_FACILITIES = 5
ALL_FACILITIES = tuple(f'{FACILITY_PREFIX}{n}' for n in range(1, MAX_FACILITIES + 1))
DEFAULT_FACILITY = ALL_FACILITIES[0]

ROUTINGS = ('patient', 'encounter')

#: HealthRecord lists whose entries may point at an encounter. An entry is sent
#: by the Edge that owns its encounter, so its EncounterNumber always resolves
#: inside the container it is sent in.
RECORD_LISTS = (
    'conditions', 'allergies', 'medications', 'procedures', 'observations',
    'careplans', 'reports', 'imaging_studies', 'devices', 'supplies',
    'immunizations',
)

MANIFEST_FIELDS = [
    'sequence', 'file', 'patient_id', 'mrn', 'family_name', 'given_name',
    'birth_time', 'alive', 'home_facility', 'encounters', 'streamlets',
    'dangling_encounter_refs',
]


@dataclass
class FeedSettings:
    """Everything a worker needs besides the generator itself."""
    facilities: List[str]
    routing: str
    output_dir: str
    only_living: bool


# ---------------------------------------------------------------------------
# Facilities and routing
# ---------------------------------------------------------------------------

def parse_facilities(value: str) -> List[str]:
    """A count (``3``) or a comma-separated list of names (``SYNTHEA_Edge2,Edge4``).

    A count takes the first N facilities in order, so ``1`` is the default
    ``SYNTHEA_Edge1`` feed on its own.
    """
    value = value.strip()
    if value.isdigit():
        count = int(value)
        if not 1 <= count <= MAX_FACILITIES:
            raise argparse.ArgumentTypeError(
                f"between 1 and {MAX_FACILITIES} facilities, not {count}")
        return list(ALL_FACILITIES[:count])

    lookup = {name.lower(): name for name in ALL_FACILITIES}
    chosen: List[str] = []
    for raw in filter(None, (part.strip() for part in value.split(','))):
        name = lookup.get(raw.lower()) or lookup.get(f'synthea_{raw}'.lower())
        if name is None:
            raise argparse.ArgumentTypeError(
                f"unknown facility {raw!r}; choose from {', '.join(ALL_FACILITIES)}")
        if name in chosen:
            raise argparse.ArgumentTypeError(f"{name} is listed twice")
        chosen.append(name)
    if not chosen:
        raise argparse.ArgumentTypeError("no facilities given")
    return chosen


def stable_pick(key: str, choices: List[str]) -> str:
    """One of `choices` for `key`, the same in every process and every run.

    ``hash()`` is salted per process, so it would route a patient differently
    in each worker; a digest does not.
    """
    digest = hashlib.sha256(key.encode('utf-8')).digest()
    return choices[int.from_bytes(digest[:8], 'big') % len(choices)]


def facility_mrn(facility: str, person) -> str:
    """A 7-digit MRN this facility would have assigned the patient."""
    digest = hashlib.sha256(f'mrn:{facility}:{person.id}'.encode('utf-8')).digest()
    return str(10 ** 6 + int.from_bytes(digest[:8], 'big') % (9 * 10 ** 6))


def _encounter_of(entry):
    return getattr(entry, 'encounter', None)


def facility_view(person, facility: str, home: str, keep: Optional[set]):
    """The patient as `facility` knows them.

    A shallow copy with its own attributes (for the facility's MRN) and, when
    `keep` is given, its own record holding only the encounters in `keep`
    and the entries recorded during them. Entries that belong to no encounter
    stay with the home facility. The generated person is never modified.
    """
    view = copy.copy(person)
    view.attributes = dict(person.attributes)
    if facility != home:
        view.attributes['identifier_mrn'] = facility_mrn(facility, person)

    if keep is not None:
        is_home = facility == home
        source = person.record
        record = copy.copy(source)
        record.encounters = [e for e in source.encounters if id(e) in keep]
        for name in RECORD_LISTS:
            entries = getattr(source, name, None)
            if entries is None:
                continue
            setattr(record, name, [
                entry for entry in entries
                if (id(_encounter_of(entry)) in keep
                    if _encounter_of(entry) is not None else is_home)
            ])
        view.record = record
    return view


def route(person, facilities: List[str],
          routing: str) -> Tuple[str, Dict[str, object]]:
    """Which facilities send this patient, and what each of them sends.

    Returns the home facility, and ``{facility: person view}`` with the home
    facility first.
    """
    fallback_home = stable_pick(f'patient:{person.id}', facilities)

    if routing == 'patient' or len(facilities) == 1:
        home = fallback_home
        return home, {home: facility_view(person, home, home, keep=None)}

    # Encounter routing: a facility (the Provider) always belongs to the same
    # Edge, across patients, as a real hospital would.
    seen: Dict[str, set] = defaultdict(set)
    for encounter in person.record.encounters:
        provider = getattr(encounter, 'provider', None)
        key = f'provider:{provider.id}' if provider is not None else f'patient:{person.id}'
        seen[stable_pick(key, facilities)].add(id(encounter))

    if seen:
        # The Edge that saw them most; ties go to the earlier-listed Edge.
        home = max(facilities, key=lambda f: (len(seen[f]), -facilities.index(f)))
    else:
        home = fallback_home

    order = [home] + [f for f in facilities if f != home and seen[f]]
    return home, {facility: facility_view(person, facility, home, keep=seen[facility])
                  for facility in order}


# ---------------------------------------------------------------------------
# Containers
# ---------------------------------------------------------------------------

def check_container(container: ET.Element, facility: str) -> Dict[str, int]:
    """Counts and the two properties HealthShare rejects a container for.

    Every EncounterNumber outside the Encounters list must name an encounter
    in the same container, and the MRN must be assigned by the sending
    facility. Splitting a record is where either could go wrong.
    """
    encounters = container.find('Encounters')
    known = set()
    if encounters is not None:
        known = {e.findtext('EncounterNumber') for e in encounters}

    dangling = 0
    streamlets = 0
    for section in container:
        if section.tag in ('SendingFacility', 'Patient'):
            continue
        streamlets += len(section)
        if section.tag == 'Encounters':
            continue
        for reference in section.iter('EncounterNumber'):
            if reference.text not in known:
                dangling += 1

    if container.findtext('SendingFacility') != facility:
        raise AssertionError(f"container is not from {facility}")
    mrn = container.find('Patient/PatientNumbers/PatientNumber')
    if mrn is None or mrn.findtext('Organization/Code') != facility:
        raise AssertionError(f"MRN is not assigned by {facility}")

    return {
        'encounters': len(known),
        'streamlets': streamlets,
        'dangling_encounter_refs': dangling,
    }


def container_filename(person) -> str:
    first = person.attributes.get('first_name') or 'Unknown'
    last = person.attributes.get('last_name') or 'Person'
    return f"{first}_{last}_{person.id[:8]}.xml"


def write_container(exporter: SDA3Exporter, container: ET.Element, path: Path) -> None:
    if exporter.pretty:
        ET.indent(container, space='  ')
    ET.ElementTree(container).write(path, encoding='utf-8', xml_declaration=True)


# ---------------------------------------------------------------------------
# Generation
# ---------------------------------------------------------------------------

class FeedWriter:
    """Generates patients and writes each into the feeds it belongs to.

    One per process: it owns a Generator (modules, providers, payers) and one
    SDA3 exporter per facility.
    """

    def __init__(self, generator: Generator, settings: FeedSettings):
        self.generator = generator
        self.settings = settings
        root = Path(settings.output_dir)
        self.exporters = {
            facility: SDA3Exporter(generator.config, root, generator.locale,
                                   sending_facility=facility)
            for facility in settings.facilities
        }
        self.directories = {}
        for facility in settings.facilities:
            directory = root / facility
            directory.mkdir(parents=True, exist_ok=True)
            self.directories[facility] = directory

    def generate(self, index: int) -> Dict:
        person = self.generator.generate_person(index)
        if person is None:
            return {'index': index, 'status': 'rejected', 'deliveries': []}
        if self.settings.only_living and not person.alive:
            return {'index': index, 'status': 'deceased_skipped', 'deliveries': []}

        deliveries = []
        home, views = route(person, self.settings.facilities, self.settings.routing)
        for facility, view in views.items():
            exporter = self.exporters[facility]
            container = exporter.create_container(view)
            checks = check_container(container, facility)
            path = self.directories[facility] / container_filename(view)
            write_container(exporter, container, path)

            patient = container.find('Patient')
            deliveries.append({
                'facility': facility,
                'sequence': index,
                'file': path.name,
                'patient_id': person.id,
                'mrn': patient.findtext('PatientNumbers/PatientNumber/Number'),
                'family_name': patient.findtext('Name/FamilyName'),
                'given_name': patient.findtext('Name/GivenName'),
                'birth_time': patient.findtext('BirthTime'),
                'alive': person.alive,
                'home_facility': home,
                **checks,
            })

        return {
            'index': index,
            'status': 'alive' if person.alive else 'dead',
            'deliveries': deliveries,
        }


_WORKER: Optional[FeedWriter] = None


def _init_worker(options: GeneratorOptions, config: Config,
                 settings: FeedSettings) -> None:
    """Build this worker process's generator and exporters once."""
    global _WORKER
    _WORKER = FeedWriter(Generator(options, config=config), settings)


def _generate_one(index: int) -> Dict:
    return _WORKER.generate(index)


def run(options: GeneratorOptions, config: Config, settings: FeedSettings,
        workers: int) -> List[Dict]:
    """Generate the population, in-process or across `workers` processes."""
    count = options.population_size
    results: List[Dict] = []

    def progress(done: int) -> None:
        if done == count or done % max(1, count // 20) == 0:
            print(f"\r  {done}/{count} patients", end='', flush=True)

    if workers <= 1:
        _init_worker(options, config, settings)
        for index in range(count):
            results.append(_generate_one(index))
            progress(len(results))
    else:
        with concurrent.futures.ProcessPoolExecutor(
                max_workers=workers, initializer=_init_worker,
                initargs=(options, config, settings)) as executor:
            chunksize = max(1, count // (workers * 4))
            for result in executor.map(_generate_one, range(count),
                                       chunksize=chunksize):
                results.append(result)
                progress(len(results))
    print()
    return results


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------

def write_manifests(results: List[Dict], settings: FeedSettings) -> Dict[str, List[Dict]]:
    """One manifest.csv per feed, in load order (patient index)."""
    by_facility: Dict[str, List[Dict]] = {f: [] for f in settings.facilities}
    for result in sorted(results, key=lambda r: r['index']):
        for delivery in result['deliveries']:
            by_facility[delivery['facility']].append(delivery)

    root = Path(settings.output_dir) 
    for facility, rows in by_facility.items():
        with open(root / facility / 'manifest.csv', 'w', newline='',
                  encoding='utf-8') as handle:
            writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS,
                                    extrasaction='ignore')
            writer.writeheader()
            writer.writerows(rows)
    return by_facility


def build_report(results: List[Dict], by_facility: Dict[str, List[Dict]],
                 settings: FeedSettings, options: GeneratorOptions,
                 workers: int, elapsed: float) -> Dict:
    statuses = Counter(r['status'] for r in results)
    spread = Counter(len(r['deliveries']) for r in results if r['deliveries'])

    feeds = {}
    for facility, rows in by_facility.items():
        feeds[facility] = {
            'directory': str(Path(settings.output_dir) / facility),
            'containers': len(rows),
            'home_patients': sum(1 for r in rows if r['home_facility'] == facility),
            'encounters': sum(r['encounters'] for r in rows),
            'streamlets': sum(r['streamlets'] for r in rows),
            'dangling_encounter_refs': sum(r['dangling_encounter_refs'] for r in rows),
        }

    return {
        'timestamp': time.strftime('%Y-%m-%d %H:%M:%S'),
        'settings': {
            **asdict(settings),
            'population_size': options.population_size,
            'seed': options.seed,
            'reference_date': options.reference_date.isoformat(),
            'state': options.state,
            'city': options.city,
            'workers': workers,
        },
        'patients': {
            'requested': options.population_size,
            'alive': statuses['alive'],
            'dead': statuses['dead'],
            'rejected': statuses['rejected'],
            'deceased_skipped': statuses['deceased_skipped'],
            # How many feeds each exported patient appears in. Always {1: n}
            # for patient routing; the MPI's workload for encounter routing.
            'facilities_per_patient': dict(sorted(spread.items())),
        },
        'feeds': feeds,
        'elapsed_seconds': round(elapsed, 2),
    }


def print_report(report: Dict) -> None:
    settings, patients = report['settings'], report['patients']
    print("\n" + "=" * 72)
    print("SDA3 Feeds")
    print("=" * 72)
    print(f"Routing: {settings['routing']}    Seed: {settings['seed']}    "
          f"Workers: {settings['workers']}    Time: {report['elapsed_seconds']}s")
    print(f"Patients: {patients['alive']} alive, {patients['dead']} dead, "
          f"{patients['rejected']} rejected"
          + (f", {patients['deceased_skipped']} deceased skipped"
             if patients['deceased_skipped'] else ''))
    if settings['routing'] == 'encounter' and len(settings['facilities']) > 1:
        spread = ', '.join(f"{n} feed(s): {count}"
                           for n, count in patients['facilities_per_patient'].items())
        print(f"Patients seen at: {spread}")

    print(f"\n{'Facility':<16}{'Containers':>12}{'Home pts':>10}"
          f"{'Encounters':>12}{'Streamlets':>12}{'Dangling':>10}")
    print("-" * 72)
    for facility, feed in report['feeds'].items():
        print(f"{facility:<16}{feed['containers']:>12}{feed['home_patients']:>10}"
              f"{feed['encounters']:>12}{feed['streamlets']:>12}"
              f"{feed['dangling_encounter_refs']:>10}")
    print()
    for facility, feed in report['feeds'].items():
        print(f"  {facility}: {feed['directory']}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def parse_args(argv=None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate synthetic patients as InterSystems SDA3 feeds, "
                    f"one per Edge ({DEFAULT_FACILITY} .. {ALL_FACILITIES[-1]}).",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument('-p', '--population', type=int, default=20,
                        help='number of patients to generate')
    parser.add_argument('-s', '--seed', type=int, default=12345,
                        help='population seed; the same seed gives the same files')
    parser.add_argument('--reference-date', type=date.fromisoformat, default=None,
                        help='simulate up to this date (YYYY-MM-DD); default is '
                             'midnight today, so reruns on one day match')
    parser.add_argument('--state', default='Massachusetts')
    parser.add_argument('--city', default=None)
    parser.add_argument('-f', '--facilities', type=parse_facilities,
                        default=[DEFAULT_FACILITY],
                        help=f'a count (1-{MAX_FACILITIES}) or a comma-separated '
                             'list of facility names')
    parser.add_argument('-r', '--routing', choices=ROUTINGS, default='patient',
                        help='patient: each patient goes to one facility; '
                             'encounter: each facility sends the visits it saw')
    parser.add_argument('-w', '--workers', type=int,
                        default=min(4, os.cpu_count() or 1),
                        help='worker processes (1 runs in-process)')
    parser.add_argument('-o', '--output', type=Path,
                        default=Path('./output/example_sda3_feeds'))
    parser.add_argument('--only-living', action='store_true',
                        help='leave deceased patients out of every feed')
    parser.add_argument('--compact', action='store_true',
                        help='write XML without indentation')
    parser.add_argument('--clean', action='store_true',
                        help='delete <output> before writing, so no file '
                             'from an earlier run is left in a feed')
    args = parser.parse_args(argv)
    if args.population < 1:
        parser.error('--population must be at least 1')
    # A worker per patient at most; each one loads every module on start-up.
    args.workers = max(1, min(args.workers, args.population))
    return args


def main(argv=None) -> int:
    args = parse_args(argv)

    print("=" * 72)
    print("Synthea Python - InterSystems SDA3 Facility Feeds")
    print("=" * 72)

    if args.clean and args.output.exists():
        shutil.rmtree(args.output)
    args.output.mkdir(parents=True, exist_ok=True)

    options = GeneratorOptions()
    options.population_size = args.population
    options.seed = args.seed
    options.state = args.state
    options.city = args.city
    options.threads = 1  # parallelism is ours, below
    # Every timestamp is relative to the reference date, which otherwise
    # defaults to the current second: two runs a moment apart would differ
    # in every file.
    options.reference_date = datetime.combine(
        args.reference_date or date.today(), datetime.min.time())
    options.reference_date_explicit = args.reference_date is not None

    # The generator's own exporters are switched off: this script decides
    # which facility each container goes to and writes them itself.
    config = Config()
    config.load()
    config.set('exporter.baseDirectory', str(args.output))
    config.set('exporter.fhir.export', False)
    config.set('exporter.sda3.export', False)
    config.set('exporter.json.export', False)
    config.set('exporter.text.export', False)
    config.set('exporter.fhir.server_url', '')
    config.set('exporter.pretty_print', not args.compact)

    settings = FeedSettings(
        facilities=args.facilities,
        routing=args.routing,
        output_dir=str(args.output),
        only_living=args.only_living,
    )

    print(f"  Population:  {args.population} ({args.state}"
          + (f", {args.city}" if args.city else '') + f"), seed {args.seed}")
    print(f"  Facilities:  {', '.join(settings.facilities)}")
    print(f"  Routing:     {settings.routing}")
    print(f"  Workers:     {args.workers}")
    print(f"  Output:      {args.output}")
    print()

    start = time.time()
    try:
        results = run(options, config, settings, args.workers)
    except KeyboardInterrupt:
        print("\nGeneration interrupted by user.")
        return 1
    elapsed = time.time() - start

    by_facility = write_manifests(results, settings)
    report = build_report(results, by_facility, settings, options,
                          args.workers, elapsed)
    report_file = args.output / 'feeds_report.json'
    report_file.write_text(json.dumps(report, indent=2), encoding='utf-8')

    print_report(report)
    print(f"\nReport: {report_file}")

    # A dangling reference is a streamlet HealthShare would drop on load.
    dangling = sum(feed['dangling_encounter_refs'] for feed in report['feeds'].values())
    if dangling:
        print(f"\n{dangling} EncounterNumber reference(s) do not resolve; "
              "see the manifests.", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
