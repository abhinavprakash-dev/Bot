"""Jokes: .joke hindi (Hindi written in English letters) and .joke eng (English).

Every server gets its own shuffled "deck": a joke is only repeated after ALL other jokes
of that language were used once. The deck is saved, so restarts do not reset it.
"""
import hashlib
import random
from typing import Any, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands

from botcore import ui

JOKES_HINDI = [
    "**Teacher:** Pappu, tumhari homework ki copy kahan hai?\n**Pappu:** Sir, mere kutte ne kha li.\n**Teacher:** Kutte ne?\n**Pappu:** Ji sir, usne pehle padhi, samajh nahi aayi, toh kha li.",
    "**Patient:** Doctor sahab, mujhe lagta hai main invisible ho gaya hoon.\n**Doctor:** Agla patient andar bhejo.",
    "**Mummy:** Beta, mood banane ke sirf do tareeke hain: padhai ya chappal.\n**Beta:** Mummy, ye toh dono hi bekaar options hain.\n**Mummy:** Isiliye toh teesra option diya hi nahi.",
    "**Husband:** Aaj khane me kya bana hai?\n**Wife:** Wahi jo tumne kal kaha tha, 'kuch bhi bana lo'.\n**Husband:** Toh?\n**Wife:** Kuch bhi nahi bana.",
    "**Mummy:** Beta uth jao, school jana hai.\n**Beta:** Mummy, mujhe school nahi jana, bachche mujhe pareshan karte hain.\n**Mummy:** Tujhe jana hi padega, tu principal hai.",
    "Maine padosi se Wi-Fi ka password pucha.\nUsne kaha: 'Router pe likha hai.'\nRouter pe likha tha: 'Password kyun chahiye?'",
    "**Teacher:** Pappu, late kyun aaye?\n**Pappu:** Sir, raste me board laga tha: 'School aage hai, dhire chalo'.",
    "**Teacher:** Agar tumhare paas 100 rupaye hain aur 50 mujhe de do, toh kitne bachenge?\n**Pappu:** 100, sir.\n**Teacher:** Kaise?\n**Pappu:** Sir, main aapko kuch deta hi nahi.",
    "**Bhikari:** Baba, kuch de do.\n**Aadmi:** Mere paas chhute nahi hain.\n**Bhikari:** Koi baat nahi, mere paas hain, main de deta hoon.",
    "**Customer:** Waiter, mere soup me makkhi kya kar rahi hai?\n**Waiter:** Sir, mujhe lagta hai backstroke.",
    "**Doctor:** Aapko roz 10 kilometer chalna hoga.\n**Patient:** Kab tak, doctor sahab?\n**Doctor:** Ek hafte tak.\n**Patient:** Phir?\n**Doctor:** Phir call karke batana kahan pahunche ho.",
    "**Interviewer:** Aapki sabse badi kamzori kya hai?\n**Candidate:** Sir, main sach bol deta hoon.\n**Interviewer:** Ye toh kamzori nahi hai.\n**Candidate:** Mujhe parwah nahi, aap kya sochte ho.",
    "**Student:** Sir, maine teen raat jag kar padhai ki.\n**Teacher:** Phir bhi result?\n**Student:** Sir, teen raat ke teen hi number aaye.",
    "**Doctor:** Aapka BP high hai.\n**Patient:** Doctor sahab, kal ghar pe bijli ka bill aaya tha.",
    "**Papa:** Beta, tu bada hokar kya banega?\n**Pappu:** Scientist, papa.\n**Papa:** Kyun?\n**Pappu:** Kyunki mujhe cheezein todna bahut achha aata hai.",
    "**Biwi:** Aap meri baat sun rahe ho?\n**Pati:** Haan.\n**Biwi:** Toh bolo maine abhi kya kaha?\n**Pati:** Mujhe pata tha tum yahi poochogi.",
    "Exam hall ka sabse bada jhoot:\n'Sir, maine sab padha tha, bas abhi yaad nahi aa raha.'",
    "Exam me teen cheezein kaam aati hain:\nKismat, guess aur 'Hey Bhagwan' ki dua.",
    "Gym join kiya tha weight kam karne ke liye.\nWeight toh kam nahi hua, par jeb ka weight bahut kam ho gaya.",
    "Maine diet kal se shuru karne ka plan banaya tha.\nPata chala, kal toh kabhi aata hi nahi.",
    "Bachpan me sochte the bade hokar free ho jayenge.\nAb pata chala free sirf Wi-Fi ka naam hota hai.",
    "Mobile ki battery 1% pe sabse zyada kaam karti hai:\nCharger dhundhna, game khelna aur video call, sab ek saath.",
    "Alarm lagaya tha subah 5 baje ka.\nAlarm ne bhi bola: 'Bhai, tu rehne de.'",
    "**Mummy:** Phone chhod aur padhai kar!\n**Main:** Mummy, phone pe padhai hi toh kar raha hoon.\n**Mummy:** Toh ye reel kisne bheji?",
    "Padosi ka bachcha roz subah 5 baje dhol bajata hai.\nMaine pucha: 'Beta, bade hokar kya banoge?'\nUsne kaha: 'Aapka dushman.'",
    "**Dost:** Yaar, 2 ghante se phone pe kya kar raha hai?\n**Main:** Kuch dekhne ke liye scroll kar raha hoon.\n**Dost:** Kya dekhna hai?\n**Main:** Pata nahi, wahi toh dhundh raha hoon.",
    "**Mummy:** Beta, kitne number aaye?\n**Beta:** Mummy, number toh bahut aaye...\n**Mummy:** Sach?\n**Beta:** Haan, par jo chahiye the wo nahi aaye.",
    "Traffic me 30 minute fanse rehne ke baad pata chala:\nGoogle Maps ne bola tha 'Aaj shortcut hai', aur shortcut ne bola 'Aaj main bhi band hoon'.",
    "**Teacher:** Taj Mahal kisne banwaya?\n**Pappu:** Sir, mazdooron ne.\n**Teacher:** Aur Shah Jahan ne?\n**Pappu:** Wo toh bas budget pass karte the.",
    "**Teacher:** Ek second me hone wali cheez batao.\n**Pappu:** Sir, ghar ki bijli jaana.",
    "**Chhota bhai:** Didi, main bade hokar aapse zyada kamaunga.\n**Didi:** Pehle apne phone ka recharge khud karwa lo.",
    "Mera phone bola: 'Storage full hai.'\nMaine kaha: 'Mere dimaag ki bhi yahi halat hai.'",
    "**Dost 1:** Kal se subah 5 baje uthkar padhunga.\n**Dost 2:** Kal bhi yahi bola tha.\n**Dost 1:** Kal bhi 5 baje hi uthna tha, bas neend 11 baje khuli.",
    "Ek machhar ne dusre se pucha: 'Aaj raat kahan chalein?'\nDusre ne kaha: 'Jahan koi 'Good night' bolkar so gaya ho.'",
    "**Teacher:** Batao, sabse zyada pyaas kise lagti hai?\n**Pappu:** Sir, mobile ko, charge ki.",
    "**Aalsi 1:** Chal race lagate hain.\n**Aalsi 2:** Theek hai.\n**Aalsi 1:** Jo pehle uthega wo haarega.\n(Dono ab tak so rahe hain.)",
    "Shaadi me plate itni bhari thi ki waiter ne pucha:\n'Sir, aap akele aaye hain ya poori baraat ke liye le ja rahe ho?'",
    "**Pappu:** Papa, aaj cricket me main out nahi hua.\n**Papa:** Sach?\n**Pappu:** Haan, stumps toh ud gaye, par bat toh salamat hai.",
    "Mere saalon ka experience bolta hai:\nJis din jaldi office pahunchne ka plan banao, usi din traffic ko bhi pata chal jaata hai.",
    "**Mobile company:** Is plan me unlimited data hai.\n**Main:** Kitna?\n**Mobile company:** Jab tak 1.5 GB roz khatam nahi hota.",
    "Sardi me bistar chhodna aur Monday ko office jana,\ndono me ek hi cheez common hai: dil toot jaata hai.",
    "Bijli ka bill dekh kar samajh aaya:\nAC bhi ek family member hai, aur sabse zyada kharcha wahi karta hai.",
    "**Dukaandaar:** Behenji, is kapde ka rang pakka hai.\n**Customer:** Pakka? Dhone ke baad bhi nahi jayega?\n**Dukaandaar:** Rang nahi jayega, bas kapda chhota ho jayega.",
    "**Papa:** Beta, itni raat tak phone pe kya kar raha hai?\n**Beta:** Papa, bas padhai kar raha hoon.\n**Papa:** Toh phir ye Instagram kyun khula hai?\n**Beta:** Papa, reference ke liye.",
    "**Teacher:** Pappu, 'chor' ko English me kya kehte hain?\n**Pappu:** Sir, 'Thief'.\n**Teacher:** Aur 'chori' ko?\n**Pappu:** Sir, 'Thieves ka kaam'.",
    "**Wife:** Main kaisi lag rahi hoon?\n**Husband:** Bilkul chand jaisi.\n**Wife:** Wah! Itni sundar?\n**Husband:** Nahi, door se hi achhi lagti ho... matlab, dil se bahut paas ho.",
    "**Dost:** Bhai, tu itna shaant kaise rehta hai?\n**Main:** Mere paas Wi-Fi hai, aur kisi se baat karne ki zaroorat nahi.",
]

JOKES_ENGLISH = [
    "Why don't scientists trust atoms?\nBecause they make up everything.",
    "I told my computer I needed a break.\nNow it keeps sending me Kit-Kat ads.",
    "Why did the scarecrow win an award?\nBecause he was outstanding in his field.",
    "I'm reading a book about anti-gravity.\nIt's impossible to put down.",
    "Why do programmers prefer dark mode?\nBecause light attracts bugs.",
    "What do you call fake spaghetti?\nAn impasta.",
    "I used to hate facial hair...\nBut then it grew on me.",
    "Why did the math book look so sad?\nIt had too many problems.",
    "How does a penguin build its house?\nIgloos it together.",
    "I asked the librarian if the library had books about paranoia.\nShe whispered, 'They're right behind you.'",
    "Why can't you trust stairs?\nThey're always up to something.",
    "What do you call a bear with no teeth?\nA gummy bear.",
    "Why did the phone go to school?\nTo get smarter.",
    "I would tell you a UDP joke...\nbut you might not get it.",
    "Why don't eggs tell jokes?\nThey'd crack each other up.",
    "What is a ghost's favorite room?\nThe living room.",
    "I'm on a seafood diet.\nI see food and I eat it.",
    "Why did the bicycle fall over?\nIt was two-tired.",
    "My Wi-Fi went down for five minutes, so I talked to my family.\nThey seem like nice people.",
    "Why did the coffee file a police report?\nIt got mugged.",
    "What do you call cheese that isn't yours?\nNacho cheese.",
    "I told my wife she was drawing her eyebrows too high.\nShe looked surprised.",
    "Why did the cookie go to the doctor?\nIt felt crummy.",
    "How do you organize a space party?\nYou planet.",
    "What did the zero say to the eight?\nNice belt!",
    "Why was the computer cold?\nIt left its Windows open.",
    "I have a joke about construction...\nbut I'm still working on it.",
    "Why do cows wear bells?\nBecause their horns don't work.",
    "What did one wall say to the other?\n'I'll meet you at the corner.'",
    "Parallel lines have so much in common.\nIt's a shame they'll never meet.",
    "I got a job at a bakery.\nBecause I kneaded the dough.",
    "What do you call a sleeping dinosaur?\nA dino-snore.",
    "My phone battery is like my motivation on Monday:\nAt 1% and still expected to work.",
    "**Teacher:** Why are you late?\n**Student:** Because of the sign.\n**Teacher:** What sign?\n**Student:** 'School ahead, go slow.'",
    "I asked my dog what two minus two is.\nHe said nothing.",
    "Why did the tomato turn red?\nBecause it saw the salad dressing.",
    "What's orange and sounds like a parrot?\nA carrot.",
    "I asked my doctor how to stop snoring.\nHe said, 'Sleep in another room.' Genius.",
    "Why don't skeletons fight each other?\nThey don't have the guts.",
    "I used to play piano by ear.\nNow I use my hands.",
    "Why did the developer go broke?\nHe used up all his cache.",
    "A SQL query walks into a bar, walks up to two tables and asks:\n'Can I join you?'",
    "There are 10 types of people in the world:\nThose who understand binary and those who don't.",
    "Why did the Java developer wear glasses?\nBecause he couldn't C#.",
    "I stayed up all night wondering where the sun went.\nThen it dawned on me.",
    "What did the ocean say to the beach?\nNothing, it just waved.",
    "Why did the picture go to jail?\nIt was framed.",
    "What do you call a fish wearing a bowtie?\nSofishticated.",
    "Why did the invisible man turn down the job offer?\nHe couldn't see himself doing it.",
    "I'm friends with 25 letters of the alphabet.\nI don't know Y.",
    "Why was six afraid of seven?\nBecause seven ate nine.",
    "I told my boss three companies were after me and I'd leave unless I got a raise.\nHe asked, 'Which three?' I said, 'Gas, electricity and water.'",
    "Why did the gym close down?\nIt just didn't work out.",
    "I'm so good at sleeping...\nI can do it with my eyes closed.",
]

POOLS = {"hindi": JOKES_HINDI, "english": JOKES_ENGLISH}
TITLES = {"hindi": "Hindi Joke", "english": "English Joke"}
ALIASES = {
    "hindi": "hindi", "hin": "hindi", "hi": "hindi", "h": "hindi",
    "eng": "english", "english": "english", "en": "english", "e": "english",
}


def joke_id(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()[:10]


class JokesCog(commands.Cog, name="Jokes"):
    def __init__(self, bot: commands.Bot, state: dict[str, Any], save: Callable[[], None]) -> None:
        self.bot = bot
        self.state = state
        self.save = save

    def pick(self, guild_id: int, language: str) -> tuple[str, int]:
        """Return (joke, jokes_left_before_a_repeat). Never repeats until the whole deck was used."""
        by_id = {joke_id(text): text for text in POOLS[language]}
        entry = self.state.setdefault("jokes", {}).setdefault(str(guild_id), {}).setdefault(language, {"used": []})
        used = [item for item in entry["used"] if item in by_id]
        remaining = [item for item in by_id if item not in used]
        if not remaining:
            # new round: the most recent third stays blocked, so a joke is never repeated soon
            used = used[-max(1, len(by_id) // 3):]
            remaining = [item for item in by_id if item not in used]
        choice = random.choice(remaining)
        used.append(choice)
        entry["used"] = used
        self.save()
        return by_id[choice], len(by_id) - len(used)

    def joke_embed(self, guild_id: int, language: str) -> discord.Embed:
        text, left = self.pick(guild_id, language)
        emb = ui.make("info", TITLES[language], text)
        emb.set_footer(text=f"{left} more jokes before any repeat • {ui.BRAND}")
        return emb

    @staticmethod
    def usage_embed() -> discord.Embed:
        return ui.info("Jokes", "Choose a language:\n`.joke hindi` : Hindi jokes in English letters\n`.joke eng` : English jokes")

    @commands.command(
        name="joke",
        aliases=["jokes"],
        brief="Get a funny joke: .joke hindi or .joke eng",
        extras={"category": "Other", "usage": ".joke hindi|eng", "public": True},
    )
    @commands.cooldown(1, 3, commands.BucketType.user)
    async def joke(self, ctx: commands.Context, language: Optional[str] = None) -> None:
        key = ALIASES.get((language or "").lower())
        if key is None:
            await ctx.send(embed=self.usage_embed())
            return
        guild_id = ctx.guild.id if ctx.guild else 0
        await ctx.send(embed=self.joke_embed(guild_id, key))

    @app_commands.command(name="joke", description="Get a funny joke in Hindi (English letters) or English")
    @app_commands.choices(
        language=[app_commands.Choice(name="Hindi", value="hindi"), app_commands.Choice(name="English", value="english")]
    )
    async def s_joke(self, interaction: discord.Interaction, language: app_commands.Choice[str]) -> None:
        guild_id = interaction.guild_id or 0
        await ui.respond(interaction, self.joke_embed(guild_id, language.value))
