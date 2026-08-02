from backend.app import (
    LibraryPolicyAnswerService,
    OCLCDiscoveryService,
    QueryPreparationService,
)


def test_unknown_availability_is_hidden():
    assert OCLCDiscoveryService._normalize_availability("Unknown") is None


def test_generic_uae_location_is_hidden():
    assert OCLCDiscoveryService._clean_patron_location("Location: UAE") is None


def test_account_request_routes_to_fees_guidance():
    assert (
        LibraryPolicyAnswerService.detect_policy_topic("I want to see my fines")
        == "account_fees"
    )


def test_account_guidance_directs_users_to_official_login():
    answer = LibraryPolicyAnswerService._static_service_message(
        "library_account", is_arabic=False
    )
    assert "WorldCat account page" in answer
    assert "UAEU credentials" in answer
    assert "cannot sign in" not in answer


def test_arabic_account_guidance_directs_users_to_official_login():
    answer = LibraryPolicyAnswerService._static_service_message(
        "library_account", is_arabic=True
    )
    assert "WorldCat" in answer
    assert "بيانات اعتماد جامعة الإمارات" in answer


def test_arabic_renewal_is_recognized_as_library_policy_query():
    assert LibraryPolicyAnswerService.might_be_policy_query("أريد تجديد كتاب")


def test_borrowed_typo_is_corrected_before_routing():
    normalized = QueryPreparationService.normalize_user_input(
        "i want to check my old broowed books"
    )
    assert normalized == "i want to check my old borrowed books"
    assert LibraryPolicyAnswerService.detect_policy_topic(normalized) == "borrowing_history"


def test_account_fines_use_the_fees_intent():
    assert (
        LibraryPolicyAnswerService.detect_policy_topic("I want to check my fines")
        == "account_fees"
    )


def test_computer_database_question_uses_recommendations():
    query = QueryPreparationService.normalize_user_input(
        "what databeses the library has for computer"
    )
    assert query == "what databases the library has for computer"
    assert (
        LibraryPolicyAnswerService.detect_policy_topic(query)
        == "database_recommendation"
    )
    answer = LibraryPolicyAnswerService._database_recommendation_answer(query)
    assert "IEEE/IET Electronic Library" in answer
    assert "ACM Digital Library" in answer


def test_common_academic_spelling_errors_are_corrected():
    query = QueryPreparationService.normalize_user_input(
        "find articals about compuer scinece and phsyics"
    )
    assert query == "find articles about computer science and physics"


def test_quoted_titles_and_author_names_are_not_spellchecked():
    query = QueryPreparationService.normalize_user_input(
        'find "The Haking Theory" by Stepehn Haking'
    )
    assert query == 'find "The Haking Theory" by Stepehn Haking'


def test_renewal_answers_include_the_official_checkouts_link():
    expected = "https://uaeu.account.worldcat.org/account/checkouts"
    assert expected in LibraryPolicyAnswerService._access_answer_en("renew books")
    assert expected in LibraryPolicyAnswerService._access_answer_ar("تجديد الكتب")
