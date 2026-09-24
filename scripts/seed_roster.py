"""The demo animal roster: every animal the seed script loads.

Kept apart from `scripts/seed.py` because it is data, not behaviour. The seed
script decides how records are written; this module only says what they are,
which lets `tests/unit/test_seed_data.py` check the roster's integrity with no
database and no network.

Three things about this data are deliberate rather than incidental:

- **The species enum has seven members, and real shelters hold more kinds than
  that.** Exotic animals therefore use `Species.OTHER` with a descriptive
  `breed` - "Bearded Dragon", "Miniature Donkey" - so the catalogue can be
  genuinely varied without widening the enum and the CHECK constraint that
  mirrors it.
- **The roster is built to exercise the matching engine** (spec section 8), not
  merely to fill a page. It deliberately contains apartment-sized low-energy
  animals, animals that need a farm, animals safe with toddlers, animals that
  must not live with children, seniors, bonded pairs and medical cases, so
  every branch of the scorer has something real to act on.
- **Descriptions are written one at a time.** Generated filler reads as
  generated filler, and spec section 24 treats the listing as part of the
  product rather than as decoration.
"""

from __future__ import annotations

from dataclasses import dataclass

from app.domain.animal_rules import AnimalSubmission, validate_animal
from app.domain.enums import (
    ActivityLevel,
    AnimalSize,
    AnimalStatus,
    Species,
    Temperament,
)

# Israeli cities the organization operates in. Animals and adopters draw from
# the same list so the location criterion sometimes matches and sometimes does
# not, which is what makes that part of the score visible in the demo.
CITIES: tuple[str, ...] = (
    "Tel Aviv",
    "Haifa",
    "Jerusalem",
    "Beer Sheva",
    "Netanya",
    "Rishon LeZion",
    "Ramat Gan",
    "Petah Tikva",
    "Ashdod",
    "Herzliya",
    "Modiin",
    "Rehovot",
    "Kfar Saba",
    "Holon",
    "Raanana",
    "Nazareth",
    "Tiberias",
    "Akko",
    "Ashkelon",
    "Eilat",
)

# Younger than any animal the shelter would list. The *upper* bound is not
# declared here: `app/domain/animal_rules.MAXIMUM_AGE_YEARS` owns it, and this
# module used to carry its own 80.0 against the domain's 40.0 - so the roster
# could have shipped a 60-year-old dog that the animal form would have
# refused, and nothing would have said so.
MINIMUM_AGE_YEARS = 0.05

# The roster declares no photograph: `scripts/seed.py` downloads one per
# animal, and FR-4.2 is enforced there. Validation still needs a URL to check,
# because an animal with no image is not listable - so a stand-in is supplied
# and the real rule is proved by the seed's own exit code.
_IMAGE_STANDIN = "/static/uploads/sample.jpg"


@dataclass(frozen=True)
class AnimalSpecification:
    """A demo animal defined before persistence.

    The fields that vary for almost every animal are positional; the ones that
    are usually true of a shelter animal - safe with children, safe with other
    pets, no medical needs - carry defaults, so an entry states only what is
    unusual about that animal. Reading the roster then shows the exceptions
    rather than burying them in a row of booleans.
    """

    name: str
    species: Species
    breed: str | None
    age_years: float
    size: AnimalSize
    temperament: Temperament
    activity_level: ActivityLevel
    required_space: AnimalSize
    description: str
    good_with_children: bool = True
    good_with_other_animals: bool = True
    has_special_needs: bool = False
    special_needs_description: str | None = None
    status: AnimalStatus | None = None


# `Species.OTHER` covers too many different animals to be a useful grouping on
# its own, so exotic breeds are also assigned a family. Used by the seed's
# summary output and by the roster tests, which require the catalogue to span
# several genuinely different kinds of animal rather than many dog breeds.
OTHER_KIND_FAMILIES: dict[str, str] = {
    "Bearded Dragon": "Reptile",
    "Leopard Gecko": "Reptile",
    "Corn Snake": "Reptile",
    "Ball Python": "Reptile",
    "Greek Tortoise": "Reptile",
    "Russian Tortoise": "Reptile",
    "Hermann's Tortoise": "Reptile",
    "Red-eared Slider": "Reptile",
    "Axolotl": "Amphibian",
    "Ferret": "Exotic Mammal",
    "Chinchilla": "Exotic Mammal",
    "African Pygmy Hedgehog": "Exotic Mammal",
    "Fancy Rat": "Exotic Mammal",
    "Fancy Mouse": "Exotic Mammal",
    "Mongolian Gerbil": "Exotic Mammal",
    "Degu": "Exotic Mammal",
    "Sugar Glider": "Exotic Mammal",
    "Nigerian Dwarf Goat": "Farm Animal",
    "Miniature Horse": "Farm Animal",
    "Pot-bellied Pig": "Farm Animal",
    "Alpaca": "Farm Animal",
    "Miniature Donkey": "Farm Animal",
    "Bantam Chicken": "Poultry",
    "Domestic Duck": "Poultry",
}


# --------------------------------------------------------------------------
# Dogs
# --------------------------------------------------------------------------

DOG_SPECIFICATIONS: tuple[AnimalSpecification, ...] = (
    AnimalSpecification(
        "Luna", Species.DOG, "Border Collie", 2.0, AnimalSize.MEDIUM,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Brilliant and tireless. Luna needs a job to do and a person who enjoys "
        "long walks.",
    ),
    AnimalSpecification(
        "Bella", Species.DOG, "Labrador Retriever", 5.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Gentle, patient and endlessly food-motivated. Wonderful with children, "
        "and completely unbothered by noise.",
    ),
    AnimalSpecification(
        "Rocky", Species.DOG, "Jack Russell Terrier", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.MEDIUM,
        "Small body, enormous personality. Rocky will out-run anyone who "
        "challenges him and has never once admitted defeat.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Atlas", Species.DOG, "German Shepherd", 6.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Loyal and highly trainable. Atlas wants a confident owner, a real "
        "routine, and to be told when he has done well.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Gus", Species.DOG, "Beagle", 8.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.MEDIUM,
        "A gentle old soul who has done his running. Now he would like a sofa, "
        "and ideally somebody on it.",
        has_special_needs=True,
        special_needs_description=(
            "Mild arthritis; needs joint supplements and two short walks rather "
            "than one long one."
        ),
    ),
    AnimalSpecification(
        "Kira", Species.DOG, "Siberian Husky", 4.0, AnimalSize.LARGE,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Needs serious exercise and a securely fenced garden. Kira will discuss "
        "any disagreement about this at length and at volume.",
    ),
    AnimalSpecification(
        "Daisy", Species.DOG, "Cavalier King Charles Spaniel", 1.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "An apartment-friendly puppy whose only real ambition is to be within "
        "arm's reach of somebody at all times.",
    ),
    AnimalSpecification(
        "Bruno", Species.DOG, "Boxer", 7.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Solid, affectionate and slightly clumsy. Bruno is convinced he is a "
        "lap dog and has never been persuaded otherwise.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Juno", Species.DOG, "Greyhound", 5.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.MEDIUM,
        "A retired racer. Juno sprints for ninety glorious seconds, then sleeps "
        "for twenty hours, which makes her a surprisingly good flatmate.",
    ),
    AnimalSpecification(
        "Hazel", Species.DOG, "Poodle", 3.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Clever and low-shedding. Hazel learns a new trick faster than most "
        "people can learn her name, and enjoys puzzle feeders.",
    ),
    AnimalSpecification(
        "Ranger", Species.DOG, "Australian Shepherd", 2.0, AnimalSize.MEDIUM,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Needs a job, a garden and a person who likes being outdoors in weather "
        "other people would call a reason to stay in.",
    ),
    AnimalSpecification(
        "Tank", Species.DOG, "Bulldog", 4.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Snores impressively enough to be heard through a closed door. Perfectly "
        "content with one short daily amble.",
        has_special_needs=True,
        special_needs_description=(
            "Brachycephalic; must avoid heat and strenuous exercise, and needs "
            "his facial folds cleaned weekly."
        ),
    ),
    AnimalSpecification(
        "Koda", Species.DOG, "Alaskan Malamute", 3.0, AnimalSize.LARGE,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Powerful, independent and an expert escape artist. Experienced owners "
        "with high fencing only, please.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Scout", Species.DOG, "Mixed Breed", 1.5, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "An adaptable, good-natured dog who fits into most households within a "
        "week. The easiest first dog on the roster.",
    ),
    AnimalSpecification(
        "Ranger II", Species.DOG, "Collie Mix", 6.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Steady, sensible and already house-trained. Named after the first "
        "Ranger by a volunteer who ran out of ideas.",
    ),
    AnimalSpecification(
        "Pixel", Species.DOG, "French Bulldog", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Pixel communicates entirely in sighs and small grumbles, and is very "
        "good company for somebody who works from home.",
        has_special_needs=True,
        special_needs_description=(
            "Brachycephalic airway; no summer midday walks, and a vet check "
            "before any anaesthetic."
        ),
    ),
    AnimalSpecification(
        "Dizzy", Species.DOG, "Boston Terrier", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Dizzy greets every visitor as though they have returned from a long "
        "war. Small enough for a flat, busy enough to need a real walk.",
    ),
    AnimalSpecification(
        "Barnaby", Species.DOG, "Basset Hound", 6.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.MEDIUM,
        "Barnaby moves at exactly one speed and follows his nose wherever it "
        "leads. Recall is theoretical.",
    ),
    AnimalSpecification(
        "Hugo", Species.DOG, "Bernese Mountain Dog", 4.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "A hundred and ten pounds of patience. Hugo leans on people he likes, "
        "which is everybody.",
    ),
    AnimalSpecification(
        "Vega", Species.DOG, "Belgian Malinois", 2.5, AnimalSize.LARGE,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Working-line and wired for it. Vega needs training, structure and a "
        "person who finds that interesting rather than exhausting.",
        good_with_children=False,
    ),
    AnimalSpecification(
        "Ozzy", Species.DOG, "Australian Cattle Dog", 3.0, AnimalSize.MEDIUM,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Ozzy will herd anything that moves, including cyclists and small "
        "children, which is charming until it isn't.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Tilly", Species.DOG, "Shetland Sheepdog", 5.0, AnimalSize.SMALL,
        Temperament.ANXIOUS, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Tilly is sensitive to noise and startles at doorbells, but she is "
        "clever, devoted, and blossoms in a quiet, predictable home.",
    ),
    AnimalSpecification(
        "Wilbur", Species.DOG, "Pug", 7.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Wilbur's hobbies are sitting in sunbeams and supervising meals. He is "
        "excellent at both.",
        has_special_needs=True,
        special_needs_description=(
            "Brachycephalic, with dry eye requiring lubricating drops twice "
            "daily."
        ),
    ),
    AnimalSpecification(
        "Ruby", Species.DOG, "Golden Retriever", 1.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.HIGH, AnimalSize.LARGE,
        "A puppy made entirely of enthusiasm and wet feet. Ruby will be a "
        "wonderful family dog once she stops eating the garden.",
    ),
    AnimalSpecification(
        "Sasha", Species.DOG, "Rottweiler", 5.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Calm, watchful and deeply attached to her person. Sasha needs an "
        "experienced owner who understands a guarding breed.",
        good_with_children=False,
    ),
    AnimalSpecification(
        "Pancake", Species.DOG, "Dachshund", 4.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Long, low and entirely convinced he is a much larger dog. Pancake "
        "barks at the postman on principle.",
        has_special_needs=True,
        special_needs_description=(
            "At risk of spinal disc disease; must be lifted rather than allowed "
            "to jump, and needs a ramp for furniture."
        ),
    ),
    AnimalSpecification(
        "Nacho", Species.DOG, "Chihuahua", 6.0, AnimalSize.SMALL,
        Temperament.ANXIOUS, ActivityLevel.LOW, AnimalSize.SMALL,
        "Nacho does not care for sudden movements, loud rooms or being picked "
        "up by strangers. Give him a fortnight and he is a different dog.",
        good_with_children=False,
    ),
    AnimalSpecification(
        "Sprout", Species.DOG, "Cockapoo", 1.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Sprout is a cheerful, low-shedding puppy who has not yet met anybody "
        "he does not consider a close friend.",
    ),
    AnimalSpecification(
        "Marla", Species.DOG, "Whippet", 3.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Marla is a quiet, thin-skinned creature who needs a blanket, a warm "
        "spot and one proper run a day.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Oakley", Species.DOG, "Labradoodle", 2.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Big, curly and unfailingly cheerful. Oakley swims at every opportunity "
        "and considers puddles opportunities.",
    ),
    AnimalSpecification(
        "Bodhi", Species.DOG, "Shiba Inu", 4.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Bodhi is clean, quiet, independent and entirely uninterested in doing "
        "anything he was not already planning to do.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Winnie", Species.DOG, "Pembroke Welsh Corgi", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Short legs, long opinions. Winnie is a herding dog in a small parcel "
        "and needs the exercise of a much bigger animal.",
    ),
    AnimalSpecification(
        "Thunder", Species.DOG, "Great Dane", 2.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Thunder is enormous, gentle and entirely unaware of his own dimensions. "
        "He will sit on you if permitted.",
    ),
    AnimalSpecification(
        "Pearl", Species.DOG, "Maltese", 9.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Pearl has spent nine years being adored and would like to continue in "
        "that line of work.",
        has_special_needs=True,
        special_needs_description=(
            "Advanced dental disease; ten teeth removed and needs soft food and "
            "daily mouth care."
        ),
    ),
    AnimalSpecification(
        "Fitz", Species.DOG, "Irish Setter", 3.0, AnimalSize.LARGE,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "All legs and optimism. Fitz needs an hour of real running a day and a "
        "person who thinks that sounds like fun.",
    ),
    AnimalSpecification(
        "Moss", Species.DOG, "Border Terrier", 5.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Scruffy, sensible and good in a flat provided he gets out twice a day. "
        "Moss has never been anything but easy.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Halva", Species.DOG, "Canaan Dog", 4.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Israel's own breed: alert, self-possessed and clean-living. Halva "
        "chooses her people carefully and keeps them.",
    ),
    AnimalSpecification(
        "Shuki", Species.DOG, "Canaan Dog Mix", 7.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.MEDIUM,
        "Shuki spent four years as a street dog in the Negev and has decided "
        "that indoor life is acceptable after all.",
    ),
    AnimalSpecification(
        "Dune", Species.DOG, "Saluki", 4.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Aloof, elegant and about as biddable as a cat. Dune will not come back "
        "if something small runs past.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Comet", Species.DOG, "Dalmatian", 3.0, AnimalSize.LARGE,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Comet was born deaf, which he does not appear to regard as a problem. "
        "He works beautifully from hand signals and watches his person closely.",
        has_special_needs=True,
        special_needs_description=(
            "Congenitally deaf; needs hand-signal training and must never be "
            "off-lead near traffic."
        ),
    ),
    AnimalSpecification(
        "Yoshi", Species.DOG, "Akita", 6.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Dignified, quiet and strongly of the view that he should be the only "
        "animal in the house. Yoshi suits one devoted adult.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Pesto", Species.DOG, "Miniature Schnauzer", 8.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.LOW, AnimalSize.SMALL,
        "Bearded, opinionated and extremely tidy. Pesto has a favourite chair "
        "and expects it to be respected.",
    ),
    AnimalSpecification(
        "Rosie", Species.DOG, "Staffordshire Bull Terrier", 4.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Rosie is a wide, wriggling, utterly people-focused dog who is far "
        "gentler with children than her reputation suggests.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Alfie", Species.DOG, "Shih Tzu", 10.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Alfie is a small, shaggy, thoroughly retired gentleman who would like a "
        "quiet flat and somebody who is usually home.",
        has_special_needs=True,
        special_needs_description=(
            "Recurrent corneal ulcers; needs daily eye drops and a vet review "
            "every three months."
        ),
    ),
    AnimalSpecification(
        "Nimbus", Species.DOG, "Samoyed", 2.0, AnimalSize.LARGE,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "A cloud with legs and an enormous grin. Nimbus sheds spectacularly and "
        "cannot cope with Israeli summer afternoons.",
    ),
    AnimalSpecification(
        "Sandy", Species.DOG, "Pitbull Terrier", 5.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Sandy has been at the shelter for eleven months because of how she "
        "looks. She is a soft, silly, couch-seeking dog.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Pippa", Species.DOG, "Papillon", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Two kilograms of butterfly-eared brilliance. Pippa learns tricks for "
        "fun and will invent her own if bored.",
    ),
    AnimalSpecification(
        "Otto", Species.DOG, "Weimaraner", 3.0, AnimalSize.LARGE,
        Temperament.ANXIOUS, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Otto is a beautiful grey shadow who cannot bear to be alone. He needs "
        "somebody at home and a great deal of exercise.",
        has_special_needs=True,
        special_needs_description=(
            "Severe separation distress; cannot be left alone for more than two "
            "hours without a behaviour plan in place."
        ),
    ),
)


# --------------------------------------------------------------------------
# Cats
# --------------------------------------------------------------------------

CAT_SPECIFICATIONS: tuple[AnimalSpecification, ...] = (
    AnimalSpecification(
        "Milo", Species.CAT, "Domestic Shorthair", 4.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "A quiet lap cat who will find the sunniest spot in any room and stay "
        "there until the sun moves.",
    ),
    AnimalSpecification(
        "Shadow", Species.CAT, "Bombay", 7.0, AnimalSize.MEDIUM,
        Temperament.ANXIOUS, ActivityLevel.LOW, AnimalSize.SMALL,
        "Shadow takes weeks to trust anybody, and rewards patience with total "
        "devotion and a great deal of purring.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Needs a quiet home; startles easily and hides for days when "
            "overwhelmed."
        ),
    ),
    AnimalSpecification(
        "Poppy", Species.CAT, "Ragdoll", 1.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Goes completely limp when picked up, exactly as the breed promises. "
        "Poppy would like to be carried everywhere.",
    ),
    AnimalSpecification(
        "Nova", Species.CAT, "Siamese", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "Nova talks constantly and expects an answer. Without stimulation she "
        "invents her own, usually involving the curtains.",
    ),
    AnimalSpecification(
        "Pumpkin", Species.CAT, "Maine Coon", 5.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Enormous, dignified, and privately convinced he is a small person with "
        "a slightly unusual job.",
    ),
    AnimalSpecification(
        "Ash", Species.CAT, "Russian Blue", 9.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Reserved and elegant. Ash prefers a calm adult household with a "
        "routine she can set her day by.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Early kidney disease; requires a prescription renal diet and blood "
            "tests twice a year."
        ),
    ),
    AnimalSpecification(
        "Sesame", Species.CAT, "Tabby", 0.6, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "A kitten operating at full power at all times, including three in the "
        "morning. Bring toys and patience.",
    ),
    AnimalSpecification(
        "Saffron", Species.CAT, "Persian", 6.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Serene, decorative and high-maintenance in the nicest possible way. "
        "Saffron accepts grooming as her due.",
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Long coat needs daily brushing to prevent painful mats; prone to "
            "tear staining that needs wiping."
        ),
    ),
    AnimalSpecification(
        "Maple", Species.CAT, "Calico", 3.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Independent but affectionate strictly on her own schedule. An easy, "
        "undemanding housemate.",
    ),
    AnimalSpecification(
        "Clementine", Species.CAT, "Scottish Fold", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Sits like a small owl and watches the room. Gentle, undemanding and "
        "very easy company.",
    ),
    AnimalSpecification(
        "Ivy", Species.CAT, "Domestic Longhair", 12.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Twelve years of experience being adored, and every intention of "
        "continuing.",
        has_special_needs=True,
        special_needs_description=(
            "Arthritic; needs a low-sided litter tray, steps to her favourite "
            "chair, and monthly pain relief."
        ),
    ),
    AnimalSpecification(
        "Gizmo", Species.CAT, "Sphynx", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Warm, wrinkled and shaped like a small gargoyle. Gizmo will get under "
        "the blanket with you whether invited or not.",
        has_special_needs=True,
        special_needs_description=(
            "Coatless; needs a warm indoor home, sun protection and a weekly "
            "bath to manage skin oils."
        ),
    ),
    AnimalSpecification(
        "Juniper", Species.CAT, "Bengal", 2.0, AnimalSize.MEDIUM,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.MEDIUM,
        "Spotted, athletic and relentless. Juniper opens doors, turns on taps, "
        "and needs climbing space more than she needs company.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Bo", Species.CAT, "British Shorthair", 4.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Round, plush and thoroughly unbothered. Bo enjoys company from a "
        "respectful distance of about one metre.",
    ),
    AnimalSpecification(
        "Freyja", Species.CAT, "Norwegian Forest Cat", 5.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "A magnificent, semi-wild-looking cat who climbs everything and sheds a "
        "second cat's worth of fur each spring.",
    ),
    AnimalSpecification(
        "Elvis", Species.CAT, "Devon Rex", 1.5, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Enormous ears, curly coat, and the social instincts of a small dog. "
        "Elvis follows his person from room to room.",
    ),
    AnimalSpecification(
        "Mochi", Species.CAT, "Munchkin", 2.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.LOW, AnimalSize.SMALL,
        "Short-legged and entirely undeterred by it. Mochi cannot jump onto the "
        "counter, which everyone considers a benefit.",
    ),
    AnimalSpecification(
        "Basil", Species.CAT, "Abyssinian", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "Basil is a ticked-coat blur who investigates everything and sits still "
        "for approximately four seconds a day.",
    ),
    AnimalSpecification(
        "Suri", Species.CAT, "Oriental Shorthair", 4.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "All angles and commentary. Suri has a great deal to say about the state "
        "of her food bowl.",
    ),
    AnimalSpecification(
        "Kiki", Species.CAT, "Turkish Angora", 6.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "White, silky and fond of water. Kiki will sit in the shower after you "
        "leave it, for reasons she has not explained.",
    ),
    AnimalSpecification(
        "Lentil", Species.CAT, "Burmese", 2.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Solid, warm and relentlessly sociable. Lentil does not understand the "
        "concept of a closed door.",
    ),
    AnimalSpecification(
        "Zohar", Species.CAT, "Domestic Shorthair", 8.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Zohar is a patient, affectionate cat who has learned to sit still for "
        "his injections and expects a treat afterwards.",
        has_special_needs=True,
        special_needs_description=(
            "Diabetic; needs insulin twice daily at fixed times and a strict "
            "feeding schedule."
        ),
    ),
    AnimalSpecification(
        "Tahini", Species.CAT, "Domestic Shorthair", 0.4, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "One half of a bonded kitten pair with Sumac. They sleep in a single "
        "heap and must be adopted together.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Sumac", Species.CAT, "Domestic Shorthair", 0.4, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "The other half of the bonded pair with Tahini. Slightly braver, "
        "slightly greedier, equally inseparable.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Blue", Species.CAT, "Nebelung", 3.0, AnimalSize.MEDIUM,
        Temperament.ANXIOUS, ActivityLevel.LOW, AnimalSize.SMALL,
        "Blue spent his first year in a hoarding case and still prefers the "
        "underside of furniture. He needs one quiet person and time.",
        good_with_children=False,
    ),
    AnimalSpecification(
        "Pixie", Species.CAT, "Singapura", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "The smallest cat in the building and the loudest voice in it. Pixie "
        "rides on shoulders.",
    ),
    AnimalSpecification(
        "Otis", Species.CAT, "Chartreux", 7.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "A blue-grey cat with a permanent half-smile who greets people at the "
        "door and then goes back to sleep.",
    ),
    AnimalSpecification(
        "Thistle", Species.CAT, "Selkirk Rex", 4.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Looks permanently surprised and feels like a sheep. Thistle is the "
        "most relaxed animal on the premises.",
    ),
    AnimalSpecification(
        "Nutmeg", Species.CAT, "Somali", 5.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "A fox-tailed, copper-coloured cat who supervises cooking from the top "
        "of the fridge.",
    ),
    AnimalSpecification(
        "Domino", Species.CAT, "Japanese Bobtail", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Black and white, short-tailed, and a committed fetcher of hair ties. "
        "Domino brings them back.",
    ),
    AnimalSpecification(
        "Cleo", Species.CAT, "Egyptian Mau", 4.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "Fast, spotted and deeply unimpressed by other cats. Cleo would like to "
        "be an only child.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Marzipan", Species.CAT, "Himalayan", 9.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Marzipan has a flat cross face and a very soft nature, and has never "
        "once jumped onto anything.",
        has_special_needs=True,
        special_needs_description=(
            "Flat-faced; needs daily eye cleaning, daily coat brushing and a "
            "raised food bowl."
        ),
    ),
    AnimalSpecification(
        "Pistachio", Species.CAT, "Tonkinese", 1.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "A young cat with the energy of a much younger cat. Pistachio plays "
        "fetch and does not stop first.",
    ),
    AnimalSpecification(
        "Nelson", Species.CAT, "Domestic Shorthair", 6.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.LOW, AnimalSize.SMALL,
        "Nelson lost an eye to an untreated infection before he arrived and has "
        "not let it slow him down in any visible way.",
        has_special_needs=True,
        special_needs_description=(
            "One eye; must be an indoor-only cat as his depth perception is "
            "poor near roads and balconies."
        ),
    ),
    AnimalSpecification(
        "Jerry", Species.CAT, "Domestic Shorthair", 3.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Three legs, four opinions. Jerry still beats every other cat here to "
        "the top of the scratching post.",
        has_special_needs=True,
        special_needs_description=(
            "Hind leg amputated; needs an indoor home with no high jumps and a "
            "weight-controlled diet."
        ),
    ),
)


# --------------------------------------------------------------------------
# Rabbits, guinea pigs and hamsters
# --------------------------------------------------------------------------

SMALL_MAMMAL_SPECIFICATIONS: tuple[AnimalSpecification, ...] = (
    AnimalSpecification(
        "Clover", Species.RABBIT, "Holland Lop", 1.5, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Litter-trained and endlessly curious. Clover is happiest with a secure "
        "pen, unlimited hay and a cardboard box to destroy.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Biscuit", Species.RABBIT, "Rex", 3.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Velvet-coated and sociable. Biscuit enjoys supervised time exploring "
        "the room and rearranging the skirting boards.",
    ),
    AnimalSpecification(
        "Willow", Species.RABBIT, "Netherland Dwarf", 0.8, AnimalSize.SMALL,
        Temperament.ANXIOUS, ActivityLevel.LOW, AnimalSize.SMALL,
        "Tiny and genuinely shy. Willow needs a gentle, quiet home and several "
        "weeks before she will take food from a hand.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Peanut", Species.RABBIT, "Lionhead", 2.5, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "A magnificent mane and an agreeable nature. Peanut tolerates being "
        "groomed, which is fortunate, because he needs it.",
    ),
    AnimalSpecification(
        "Bramble", Species.RABBIT, "Flemish Giant", 4.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.MEDIUM,
        "Seven kilograms of astonishingly placid rabbit. Bramble needs far more "
        "space than anybody expects - closer to a medium dog's.",
    ),
    AnimalSpecification(
        "Marigold", Species.RABBIT, "English Lop", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Bonded for life with Sorrel; the two groom each other constantly and "
        "must be adopted as a pair.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Sorrel", Species.RABBIT, "English Lop", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Marigold's bonded partner, and the braver of the two. Separating them "
        "would distress both.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Dandelion", Species.RABBIT, "Mini Rex", 1.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Dandelion performs a full mid-air twist when pleased, which is most of "
        "the time.",
    ),
    AnimalSpecification(
        "Nugget", Species.RABBIT, "Dutch Rabbit", 5.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Nugget carries his head at a permanent tilt after an old infection, "
        "and manages perfectly well at a slightly jaunty angle.",
        has_special_needs=True,
        special_needs_description=(
            "Permanent head tilt from a past E. cuniculi infection; needs a "
            "single-level pen and food placed where he can reach it."
        ),
    ),
    AnimalSpecification(
        "Barley", Species.RABBIT, "Continental Giant", 3.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.MEDIUM,
        "Larger than most cats and softer than any of them. Barley needs a room "
        "rather than a hutch.",
    ),
    AnimalSpecification(
        "Tulip", Species.RABBIT, "Angora Rabbit", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Tulip looks like a cloud and requires the upkeep of one. Beautiful, "
        "affectionate and a genuine commitment.",
        has_special_needs=True,
        special_needs_description=(
            "Wool coat must be groomed every two to three days and clipped "
            "seasonally, or it mats to the skin."
        ),
    ),
    AnimalSpecification(
        "Olive", Species.GUINEA_PIG, "Abyssinian", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Chatty in the best way. Guinea pigs do far better in pairs, and Olive "
        "would agree loudly.",
    ),
    AnimalSpecification(
        "Tofu", Species.GUINEA_PIG, "American", 1.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Squeaks at the sound of the fridge opening from three rooms away. A "
        "cheerful, straightforward first pet.",
    ),
    AnimalSpecification(
        "Wren", Species.GUINEA_PIG, "Peruvian", 1.5, AnimalSize.SMALL,
        Temperament.ANXIOUS, ActivityLevel.LOW, AnimalSize.SMALL,
        "A little timid at first, and then completely yours. Wren bonds to a "
        "voice before she bonds to a hand.",
    ),
    AnimalSpecification(
        "Churro", Species.GUINEA_PIG, "Teddy", 0.8, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Wiry-coated and permanently hungry. Bonded with Concha and adopted "
        "only alongside her.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Concha", Species.GUINEA_PIG, "Teddy", 0.8, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Churro's sister and co-conspirator. She popcorns sideways across the "
        "hutch when the hay arrives.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Kernel", Species.GUINEA_PIG, "Silkie", 3.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Long-coated, patient and content to sit on a lap under a towel for an "
        "entire film.",
    ),
    AnimalSpecification(
        "Rufus", Species.GUINEA_PIG, "Rex", 4.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "An older boy with a grey muzzle and a great deal of dignity, provided "
        "there is a cucumber involved.",
        has_special_needs=True,
        special_needs_description=(
            "History of bladder stones; needs a low-calcium diet, extra water "
            "and a urine check twice a year."
        ),
    ),
    AnimalSpecification(
        "Zuzu", Species.GUINEA_PIG, "Skinny Pig", 1.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Almost entirely hairless and therefore almost entirely warm to the "
        "touch. Zuzu is a small pink hot-water bottle.",
        has_special_needs=True,
        special_needs_description=(
            "Hairless; needs a room kept above 20C, fleece bedding and no "
            "direct sunlight."
        ),
    ),
    AnimalSpecification(
        "Pepper", Species.HAMSTER, "Syrian", 0.5, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "A tidy nocturnal companion who stores sunflower seeds in one specific "
        "corner and audits them nightly.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Pip", Species.HAMSTER, "Dwarf Campbell", 0.4, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "Fast, tiny and extremely fond of the wheel at three in the morning. "
        "Not a bedroom animal.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Fig", Species.HAMSTER, "Roborovski", 0.3, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "The smallest and fastest hamster here. Better watched through glass "
        "than handled.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Truffle", Species.HAMSTER, "Syrian", 1.2, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Unusually placid for a hamster. Truffle will sit in a cupped hand and "
        "wash her face while you watch.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Cinnamon", Species.HAMSTER, "Chinese Hamster", 0.6, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Slim, long-tailed and a capable climber. Cinnamon will use every "
        "centimetre of vertical space you give her.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Butterscotch", Species.HAMSTER, "Winter White", 0.9, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Turns paler in winter, as advertised. A quiet, undemanding animal for "
        "somebody who keeps late hours.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Moby", Species.HAMSTER, "Syrian", 1.8, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "An elderly hamster who has slowed to a gentle shuffle and would like "
        "to spend his remaining months somewhere warm.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Recovering from a wet-tail episode; needs a strict dry diet and a "
            "weekly weight check."
        ),
    ),
)


# --------------------------------------------------------------------------
# Birds
# --------------------------------------------------------------------------

BIRD_SPECIFICATIONS: tuple[AnimalSpecification, ...] = (
    AnimalSpecification(
        "Ziggy", Species.BIRD, "Cockatiel", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Whistles a tune he appears to have invented himself and repeats it "
        "until complimented. Enjoys out-of-cage time.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Mango", Species.BIRD, "Budgerigar", 1.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Bright, busy and bonded to Kiwi. Budgies are flock birds and these two "
        "leave together.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Kiwi", Species.BIRD, "Budgerigar", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Mango's constant companion and the more talkative of the two. Has "
        "learned to imitate a microwave.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Echo", Species.BIRD, "African Grey Parrot", 11.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Echo has a vocabulary, a sense of timing and firm opinions. This is a "
        "serious commitment measured in decades.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Highly intelligent; needs daily interaction and rotating "
            "enrichment or he returns to feather-plucking."
        ),
    ),
    AnimalSpecification(
        "Sinatra", Species.BIRD, "Canary", 2.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.LOW, AnimalSize.SMALL,
        "Sings for about four hours a day and does not take requests. Prefers "
        "to be admired rather than handled.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Beatrix", Species.BIRD, "Zebra Finch", 1.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.LOW, AnimalSize.SMALL,
        "Beatrix beeps like a small toy and never stops moving. Finches need "
        "company of their own kind rather than yours.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Rainbow", Species.BIRD, "Gouldian Finch", 1.5, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.LOW, AnimalSize.SMALL,
        "Improbably coloured, as though somebody had painted her by committee. "
        "Delicate and best kept warm.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Solomon", Species.BIRD, "Society Finch", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "A gentle, sociable little bird who has fostered three orphaned "
        "clutches for us and never complained.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Noodle", Species.BIRD, "Green-cheeked Conure", 4.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "Noodle hangs upside down from everything and sleeps in a shirt pocket. "
        "Small, loud and hilarious.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Sunny", Species.BIRD, "Sun Conure", 6.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.MEDIUM,
        "Spectacular to look at and genuinely deafening. Sunny needs a house "
        "rather than a flat with shared walls.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Extremely loud contact calls; unsuitable for attached housing and "
            "needs several hours of interaction daily."
        ),
    ),
    AnimalSpecification(
        "Raja", Species.BIRD, "Indian Ringneck Parakeet", 5.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Elegant, clever and going through what his keeper calls a phase. He "
        "says his own name and little else.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Winston", Species.BIRD, "Quaker Parrot", 8.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Winston builds nests out of anything available and mutters to himself "
        "while doing it. A remarkable talker.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Coco", Species.BIRD, "Sulphur-crested Cockatoo", 22.0, AnimalSize.MEDIUM,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.MEDIUM,
        "Coco is twenty-two and may outlive her next owner. She dances, screams "
        "and has been rehomed three times already.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Established feather-plucking and separation screaming; needs an "
            "experienced parrot home with a written long-term care plan."
        ),
    ),
    AnimalSpecification(
        "Blossom", Species.BIRD, "Galah Cockatoo", 14.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Pink, grey and unshakeably cheerful. Blossom will hang off a finger and "
        "ask to have her crest scratched.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Esther", Species.BIRD, "Eclectus Parrot", 9.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "A deep red parrot with an unexpectedly calm manner and a highly "
        "specific diet of fresh fruit and vegetables.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Yona", Species.BIRD, "Ringneck Dove", 3.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Coos gently at dawn and dusk and is otherwise entirely undemanding. "
        "The quietest bird in the building.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Snowdrop", Species.BIRD, "Diamond Dove", 1.5, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Barely larger than a sparrow, with a ring of orange around each eye. "
        "Gentle, decorative and best kept in a pair.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Mazal", Species.BIRD, "Lovebird", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Bonded to Tikva and utterly miserable apart from her. The two preen "
        "each other for hours.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Tikva", Species.BIRD, "Lovebird", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Mazal's partner, and the one who bites first and apologises later. "
        "They must be adopted together.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
)


# --------------------------------------------------------------------------
# Reptiles, amphibians, exotic mammals and farm animals
# --------------------------------------------------------------------------

EXOTIC_SPECIFICATIONS: tuple[AnimalSpecification, ...] = (
    AnimalSpecification(
        "Smaug", Species.OTHER, "Bearded Dragon", 3.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Sits on a shoulder like a small dinosaur and waves one arm at his own "
        "reflection. Genuinely tame, and already has his 120cm vivarium, "
        "basking lamp and UVB tube to come with him.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Pebble", Species.OTHER, "Leopard Gecko", 4.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Permanently smiling, entirely nocturnal, and happy to be handled for "
        "ten minutes at a time. An excellent first reptile.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Cornelius", Species.OTHER, "Corn Snake", 5.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "A calm, orange, escape-minded snake who has been recaptured from behind "
        "the same radiator twice. Check your latches.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Monty", Species.OTHER, "Ball Python", 7.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Spends most of his life in a tidy coil and the rest of it deciding "
        "whether to eat. Heavy, gentle and undemanding.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Refuses food for weeks at a time each winter; needs an owner who "
            "will monitor weight rather than panic."
        ),
    ),
    AnimalSpecification(
        "Methuselah", Species.OTHER, "Greek Tortoise", 34.0, AnimalSize.MEDIUM,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.MEDIUM,
        "Thirty-four years old and barely middle-aged. Methuselah will need to "
        "be written into somebody's will.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Gilgamesh", Species.OTHER, "Russian Tortoise", 12.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Small, stubborn and surprisingly quick when there are dandelions "
        "involved. Digs under anything left unsecured.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Herschel", Species.OTHER, "Hermann's Tortoise", 8.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "Follows footsteps around the garden hoping to be fed, and is usually "
        "right to hope.",
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Nefertiti", Species.OTHER, "Red-eared Slider", 15.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.LOW, AnimalSize.MEDIUM,
        "Surrendered when she outgrew her second tank. She is the size of a "
        "dinner plate and still growing.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Needs a 400-litre aquatic setup with strong filtration, a dry "
            "basking platform and a UVB lamp."
        ),
    ),
    AnimalSpecification(
        "Axel", Species.OTHER, "Axolotl", 2.0, AnimalSize.SMALL,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.SMALL,
        "A permanently smiling aquatic salamander with frilled pink gills. "
        "Fascinating to watch, and never to be handled.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Needs cold water held between 16C and 18C with a chiller in "
            "summer; handling damages his skin."
        ),
    ),
    AnimalSpecification(
        "Bandit", Species.OTHER, "Ferret", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "Steals socks, hides them under the sofa, and performs a delighted "
        "sideways dance when caught. Bonded with Loki.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Loki", Species.OTHER, "Ferret", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.SMALL,
        "Bandit's partner in every crime so far recorded. Ferrets are social "
        "and these two go as a pair.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Dusty", Species.OTHER, "Chinchilla", 6.0, AnimalSize.SMALL,
        Temperament.ANXIOUS, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "The softest animal in the building and the least interested in being "
        "touched. Dusty prefers to be watched bathing in dust.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Cannot tolerate heat above 25C; needs an air-conditioned room and "
            "a dust bath three times a week."
        ),
    ),
    AnimalSpecification(
        "Thorn", Species.OTHER, "African Pygmy Hedgehog", 2.0, AnimalSize.SMALL,
        Temperament.ANXIOUS, ActivityLevel.LOW, AnimalSize.SMALL,
        "Huffs, clicks and rolls into a ball when startled, which is often. "
        "Wins nobody over quickly and everybody over eventually.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Remy", Species.OTHER, "Fancy Rat", 1.5, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Clever enough to be genuinely good company. Remy comes when called and "
        "grooms his person's fingers. Bonded with Emile.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Emile", Species.OTHER, "Fancy Rat", 1.5, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Remy's brother, and the one who works out how the door catch opens. "
        "Rats should never be kept singly.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Crumb", Species.OTHER, "Fancy Mouse", 0.7, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Weighs less than a slice of bread and builds elaborate tunnels through "
        "her bedding every single night.",
        good_with_children=False,
        good_with_other_animals=False,
    ),
    AnimalSpecification(
        "Sahara", Species.OTHER, "Mongolian Gerbil", 1.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Digs with total commitment and needs deep bedding to do it properly. "
        "Bonded with Negev since birth.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Negev", Species.OTHER, "Mongolian Gerbil", 1.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "Sahara's brother. Gerbils separated from their pair-mate rarely settle, "
        "so these two leave together.",
        good_with_other_animals=False,
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Pilar", Species.OTHER, "Degu", 2.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "A diurnal South American rodent who chirps conversationally and is "
        "awake at the same hours you are.",
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Cannot metabolise sugar; any fruit or sweet treat risks diabetes "
            "and cataracts, so the diet must be strictly controlled."
        ),
    ),
    AnimalSpecification(
        "Bindi", Species.OTHER, "Sugar Glider", 3.0, AnimalSize.SMALL,
        Temperament.ENERGETIC, ActivityLevel.MODERATE, AnimalSize.SMALL,
        "A nocturnal marsupial who glides between curtain rails and sleeps in a "
        "cloth pouch all day.",
        good_with_children=False,
        good_with_other_animals=False,
        has_special_needs=True,
        special_needs_description=(
            "Colony animal that must not be kept alone; needs a tall aviary-style "
            "cage and a specialist insect and nectar diet."
        ),
    ),
    AnimalSpecification(
        "Gimel", Species.OTHER, "Nigerian Dwarf Goat", 2.0, AnimalSize.MEDIUM,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Climbs on everything, including the car. Goats are herd animals, so "
        "Gimel leaves with Dalet or not at all.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Dalet", Species.OTHER, "Nigerian Dwarf Goat", 2.0, AnimalSize.MEDIUM,
        Temperament.ENERGETIC, ActivityLevel.HIGH, AnimalSize.LARGE,
        "Gimel's herd-mate and the quieter of the two, which is not saying very "
        "much. Needs secure fencing and company.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Duchess", Species.OTHER, "Miniature Horse", 9.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Eighty centimetres at the shoulder and entirely in charge. Duchess has "
        "done therapy visits and adores being brushed.",
    ),
    AnimalSpecification(
        "Babka", Species.OTHER, "Pot-bellied Pig", 4.0, AnimalSize.LARGE,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Intelligent, affectionate, and considerably larger than the "
        "'miniature' pig her first owner was promised.",
        has_special_needs=True,
        special_needs_description=(
            "Needs a vet who treats pigs, hoof trimming twice a year and a "
            "strictly measured diet; she will eat until she cannot walk."
        ),
    ),
    AnimalSpecification(
        "Golda", Species.OTHER, "Bantam Chicken", 2.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Lays small cream eggs, follows people around the yard, and roosts on "
        "the fence at sunset. Leaves with Shoshana.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Shoshana", Species.OTHER, "Bantam Chicken", 2.0, AnimalSize.SMALL,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.MEDIUM,
        "Golda's flockmate and the undisputed senior hen. Chickens are flock "
        "birds and should never be kept alone.",
        status=AnimalStatus.AVAILABLE,
    ),
    AnimalSpecification(
        "Mallory", Species.OTHER, "Domestic Duck", 3.0, AnimalSize.MEDIUM,
        Temperament.BALANCED, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Needs water deep enough to swim in and mud to dabble in. Mallory is "
        "charming, messy and extremely loud at dawn.",
    ),
    AnimalSpecification(
        "Inca", Species.OTHER, "Alpaca", 5.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.MODERATE, AnimalSize.LARGE,
        "Gentle, watchful and curious from a safe distance. Alpacas are herd "
        "animals and need at least one companion of their own kind.",
    ),
    AnimalSpecification(
        "Amos", Species.OTHER, "Miniature Donkey", 11.0, AnimalSize.LARGE,
        Temperament.CALM, ActivityLevel.LOW, AnimalSize.LARGE,
        "Brays at the gate every morning until greeted. Amos is patient, "
        "stubborn and wonderful with visiting children.",
    ),
)


ANIMAL_SPECIFICATIONS: tuple[AnimalSpecification, ...] = (
    DOG_SPECIFICATIONS
    + CAT_SPECIFICATIONS
    + SMALL_MAMMAL_SPECIFICATIONS
    + BIRD_SPECIFICATIONS
    + EXOTIC_SPECIFICATIONS
)


# A repeating pattern rather than a list the length of the roster, so adding
# animals cannot silently leave the tail of the roster all AVAILABLE. Roughly
# three quarters available, which is what a working shelter looks like and what
# gives the dashboard non-zero figures in every column.
STATUS_CYCLE: tuple[AnimalStatus, ...] = (
    AnimalStatus.AVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.RESERVED,
    AnimalStatus.AVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.ADOPTED,
    AnimalStatus.AVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.ADOPTION_IN_PROGRESS,
    AnimalStatus.AVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.UNAVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.ADOPTED,
    AnimalStatus.AVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.RESERVED,
    AnimalStatus.AVAILABLE,
    AnimalStatus.AVAILABLE,
    AnimalStatus.ADOPTION_IN_PROGRESS,
    AnimalStatus.AVAILABLE,
)


def status_for(specification: AnimalSpecification, roster_index: int) -> AnimalStatus:
    """Decide an animal's pipeline status (spec section 25).

    An animal that pins its own status keeps it - bonded pairs are pinned to
    AVAILABLE so a demo never shows one half of a pair adopted and the other
    half waiting. Everything else takes its status from a repeating pattern,
    which keeps the spread realistic and identical on every run.

    Args:
        specification: The animal being persisted.
        roster_index: Its position in `ANIMAL_SPECIFICATIONS`.

    Returns:
        The status to store.
    """
    if specification.status is not None:
        return specification.status
    return STATUS_CYCLE[roster_index % len(STATUS_CYCLE)]


def kind_group(specification: AnimalSpecification) -> str:
    """Name the kind of animal this is, for grouped reporting.

    `Species.OTHER` spans reptiles, farm animals and exotic mammals, which are
    not one group in any useful sense, so OTHER animals report their family
    instead (spec section 5.2 leaves the species vocabulary fixed).

    Args:
        specification: The animal to classify.

    Returns:
        A species value, or a family name for exotic animals.
    """
    if specification.species is not Species.OTHER:
        return specification.species.value
    return OTHER_KIND_FAMILIES.get(specification.breed or "", "OTHER")


def validate_animal_specification(specification: AnimalSpecification) -> tuple[str, ...]:
    """Report everything wrong with one roster entry.

    The roster is hand-written data, and a hand-written mistake - a blank
    description, an exotic animal with no breed to display, a special-needs
    flag with nothing to explain it - would reach the interface as a broken
    listing rather than as an error. This is what
    `tests/unit/test_seed_data.py` runs over every entry.

    The entry is put through `app.domain.animal_rules.validate_animal`, the
    same validator a staff member's form goes through, so seeded data cannot
    be something the application would have rejected. That is where the age
    ceiling and every column width come from; this module's own checks are
    only the ones the form does not make - a description is optional on the
    form and mandatory in a demo, and an `OTHER` animal needs a breed for the
    interface to have anything to call it.

    Args:
        specification: The entry to check.

    Returns:
        A tuple of problem descriptions, empty when the entry is sound.
    """
    problems = list(_roster_only_problems(specification))
    problems.extend(
        f"{field}: {message}"
        for field, message in sorted(validate_animal(_as_submission(specification)).errors.items())
    )
    return tuple(problems)


def _as_submission(specification: AnimalSpecification) -> AnimalSubmission:
    """Shape one roster entry the way the animal form's validator reads it.

    The city and the photograph are supplied by the seed rather than declared
    per animal, so a plausible stand-in is used for each: neither is a
    property of the roster entry being checked.
    """
    return AnimalSubmission(
        name=specification.name,
        species=specification.species.value,
        breed=specification.breed,
        age_years=str(specification.age_years),
        size=specification.size.value,
        temperament=specification.temperament.value,
        activity_level=specification.activity_level.value,
        required_space=specification.required_space.value,
        city=CITIES[0],
        status=specification.status.value if specification.status else None,
        description=specification.description,
        good_with_children=specification.good_with_children,
        good_with_other_animals=specification.good_with_other_animals,
        has_special_needs=specification.has_special_needs,
        special_needs_description=specification.special_needs_description,
        image_urls=(_IMAGE_STANDIN,),
    )


def _roster_only_problems(specification: AnimalSpecification) -> tuple[str, ...]:
    """The checks the animal form does not make, but a demo roster needs."""
    problems: list[str] = []

    if not specification.description.strip():
        problems.append("description is empty")
    if specification.species is Species.OTHER and not (specification.breed or "").strip():
        problems.append("an OTHER animal needs a breed so the interface can name it")
    if specification.age_years < MINIMUM_AGE_YEARS:
        problems.append(f"age {specification.age_years} is younger than any listed animal")

    description = (specification.special_needs_description or "").strip()
    if description and not specification.has_special_needs:
        # The form drops this silently, which is right for a form and wrong
        # for hand-written data: the note was written on purpose.
        problems.append("special needs are described but not flagged")
    return tuple(problems)
