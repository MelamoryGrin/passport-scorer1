from datetime import datetime, timezone

import api_logging as logging
from account.deduplication import Rules
from account.deduplication.fifo import fifo
from account.deduplication.lifo import lifo
from account.models import AccountAPIKey, AccountAPIKeyAnalytics, Community
from asgiref.sync import async_to_sync
from celery import shared_task
from ninja_extra.exceptions import APIException
from reader.passport_reader import get_did, get_passport
from registry.exceptions import NoPassportException
from registry.models import Passport, Score, Stamp
from registry.utils import validate_credential, verify_issuer

log = logging.getLogger(__name__)


def get_utc_time():
    return datetime.now(timezone.utc)


@shared_task
def save_api_key_analytics(api_key_id, path):
    try:
        AccountAPIKeyAnalytics.objects.create(
            api_key_id=api_key_id,
            path=path,
        )
    except Exception as e:
        pass


def asave_api_key_analytics(api_key_id, path):
    try:
        AccountAPIKeyAnalytics.objects.acreate(
            api_key_id=api_key_id,
            path=path,
        )
    except Exception as e:
        pass


@shared_task
def score_passport_passport(community_id: int, address: str):
    score_passport(community_id, address)


@shared_task
def score_registry_passport(community_id: int, address: str):
    score_passport(community_id, address)


def score_passport(community_id: int, address: str):
    log.info(
        "score_passport request for community_id=%s, address='%s'",
        community_id,
        address,
    )

    passport = None
    try:
        passport = load_passport_record(community_id, address)
        if not passport:
            log.info(
                "Passport no passport found for address='%s', community_id='%s' that has requires_calculation=True or None",
                address,
                community_id,
            )
            return
        remove_existing_stamps_from_db(passport)
        passport_data = load_passport_data(address)
        validate_and_save_stamps(passport, passport_data)
        calculate_score(passport, community_id)

    except APIException as e:
        log.error(
            "APIException when handling passport submission. community_id=%s, address='%s'",
            community_id,
            address,
            exc_info=True,
        )
        if passport:
            # Create a score with error status
            Score.objects.update_or_create(
                passport_id=passport.pk,
                defaults=dict(
                    score=None,
                    status=Score.Status.ERROR,
                    last_score_timestamp=None,
                    evidence=None,
                    error=e.detail,
                ),
            )
    except Exception as e:
        log.error(
            "Error when handling passport submission. community_id=%s, address='%s'",
            community_id,
            address,
            exc_info=True,
        )
        if passport:
            # Create a score with error status
            Score.objects.update_or_create(
                passport_id=passport.pk,
                defaults=dict(
                    score=None,
                    status=Score.Status.ERROR,
                    last_score_timestamp=None,
                    evidence=None,
                    error=str(e),
                ),
            )


def load_passport_data(address: str):
    # Get the passport data from the blockchain or ceramic cache
    passport_data = get_passport(address)
    if not passport_data:
        raise NoPassportException()

    return passport_data


def load_passport_record(community_id: int, address: str) -> Passport | None:
    # A Passport instance should exist, and have the requires_calculation flag set to True if it requires calculation.
    # We check for this by running an update and checking for the number of updated rows
    # This update should also avoid race conditions as stated in the documentation: https://docs.djangoproject.com/en/4.2/ref/models/querysets/#update
    # We query for all passports that have requires_calculation not set to False
    # because we want to calculate the score for any passport that has requires_calculation set to True or None
    num_passports_updated = (
        Passport.objects.filter(address=address.lower(), community_id=community_id)
        .exclude(requires_calculation=False)
        .update(requires_calculation=False)
    )

    # If the num_passports_updated == 1, this means we are in the lucky task that has managed to pick this passport up for processing
    # Other tasks which are potentially racing for the same calculation should get num_passports_updated == 0
    if num_passports_updated == 1:
        db_passport = Passport.objects.get(
            address=address.lower(),
            community_id=community_id,
        )
        return db_passport
    else:
        # Just in case the Passport does not exist, we create it
        if not Passport.objects.filter(
            address=address.lower(), community_id=community_id
        ).exists():
            db_passport, _ = Passport.objects.update_or_create(
                address=address.lower(), community_id=community_id
            )
            return db_passport
    return None


def process_deduplication(passport, passport_data):
    """
    Process deduplication based on the community rule
    """
    rule_map = {
        Rules.LIFO.value: lifo,
        Rules.FIFO.value: fifo,
    }

    method = rule_map.get(passport.community.rule)

    log.debug(
        "Processing deduplication for address='%s' and method='%s'",
        passport.address,
        method,
    )

    if not method:
        raise Exception("Invalid rule")

    deduplicated_passport, affected_passports = method(
        passport.community, passport_data, passport.address
    )

    log.debug(
        "Processing deduplication found deduplicated_passport='%s' and affected_passports='%s'",
        deduplicated_passport,
        affected_passports,
    )

    # If the rule is FIFO, we need to re-score all affected passports
    if passport.community.rule == Rules.FIFO.value:
        for passport in affected_passports:
            log.debug(
                "FIFO scoring selected, rescoring passport='%s'",
                passport,
            )

            Score.objects.update_or_create(
                passport=passport,
                defaults=dict(score=None, status=Score.Status.PROCESSING),
            )
            calculate_score(passport, passport.community_id)

    return deduplicated_passport


def validate_and_save_stamps(passport: Passport, passport_data):
    log.debug("getting stamp data ")

    log.debug("processing deduplication")

    deduped_passport_data = process_deduplication(passport, passport_data)

    log.debug("validating stamps")
    did = get_did(passport.address)

    for stamp in deduped_passport_data["stamps"]:
        stamp_return_errors = async_to_sync(validate_credential)(
            did, stamp["credential"]
        )
        try:
            # TODO: use some library or https://docs.python.org/3/library/datetime.html#datetime.datetime.fromisoformat to
            # parse iso timestamps
            stamp_expiration_date = datetime.strptime(
                stamp["credential"]["expirationDate"], "%Y-%m-%dT%H:%M:%S.%fZ"
            )
        except ValueError:
            stamp_expiration_date = datetime.strptime(
                stamp["credential"]["expirationDate"], "%Y-%m-%dT%H:%M:%SZ"
            )

        is_issuer_verified = verify_issuer(stamp)
        # check that expiration date is not in the past
        stamp_is_expired = stamp_expiration_date < datetime.now()
        if (
            len(stamp_return_errors) == 0
            and not stamp_is_expired
            and is_issuer_verified
        ):
            Stamp.objects.update_or_create(
                hash=stamp["credential"]["credentialSubject"]["hash"],
                passport=passport,
                defaults={
                    "provider": stamp["provider"],
                    "credential": stamp["credential"],
                },
            )
        else:
            log.info(
                "Stamp not created. Stamp=%s\nReason: errors=%s stamp_is_expired=%s is_issuer_verified=%s",
                stamp,
                stamp_return_errors,
                stamp_is_expired,
                is_issuer_verified,
            )


def remove_existing_stamps_from_db(passport: Passport):
    Stamp.objects.filter(passport=passport).delete()


def calculate_score(passport: Passport, community_id: int):
    log.debug("Scoring")
    user_community = Community.objects.get(pk=community_id)

    scorer = user_community.get_scorer()
    scores = scorer.compute_score([passport.pk])

    log.info("Scores for address '%s': %s", passport.address, scores)
    scoreData = scores[0]

    Score.objects.update_or_create(
        passport_id=passport.pk,
        defaults=dict(
            score=scoreData.score,
            status=Score.Status.DONE,
            last_score_timestamp=get_utc_time(),
            evidence=scoreData.evidence[0].as_dict() if scoreData.evidence else None,
            error=None,
        ),
    )
