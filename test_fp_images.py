"""
Test the false-positive images (predicted crash, GT = pulled_over or unknown_incident)
against the updated prompt to verify the crash precision fix.

Usage:
    export ANTHROPIC_AUTH_TOKEN="sk-..."
    python3 test_fp_images.py
    python3 test_fp_images.py --model openai/gpt-4o-mini
    python3 test_fp_images.py --model openai/gpt-4o
"""

import argparse, base64, sys, os, time, urllib.request

try:
    import openai
except ImportError:
    os.system(f"{sys.executable} -m pip install openai -q")
    import openai

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from test_single_image import HEADLINE_PROMPT, LITELLM_PROXY_URL

VALID_SEVERITIES = {"general.alert2.local", "general.alert3"}

# (label, gt, url)
IMAGES = [
    ("01", "pulled_over",     "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/7/yrp9p4C7X4QdYZ0cS6RvavCHhns0.jpg?Expires=1815048201&Signature=v7BcspJvxs6ZfeG~we4HAwGUusslh~55KGlIT2QLL3qaBPp0e7x6pkXLYZDZ3jXlx6uBLFyKNS8onlCh3i6COZpdPkRTYBedDlwLFWPJqyXU~aHXPNk2uY1eKJWGm9CVQKD1XPFBECN~QEe-Oq75Dr1spSmTY9X6C7PxW4Rjdm0R1ZZgUpzECPQTmrJ1AHTMujwd6vI98PDF1S~VnY~KNxVxZtqMlmmUCZyE4TrJyOKRBqt-5CMqVrxdfHSOHZL~LfnWsvT2dUMxP6swEutxgA7ta-wdAXaWocgZ5O5Be~ZfYmY1b-xjkqQHja9-tHXxvO3G5K5VIImD~GJwY0It0A__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("02", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/7/Mbon9P3zK33M5kZIrbAOgpfUtNM0.jpg?Expires=1815047032&Signature=nvoMoYHe~1XnqgQF-RQJdgo-5mG0bFzjcnDbivu2hKjCSMI6mth~TNgoxA7Lh2QuAC~Pao1VzjZAsmN82Y3ObO3~RDgcXeqO~DNEeeESGBedp-ndF-teTgcQ4SIJAe92gXJdAZS7z-5z4kdTeyMBnsqQ7m3F8V~aHGK9v0w9pmW9kNCwhC~kS51irD6nok3wUNEvJpaBd6cXC4~Ry2o~fAt-9geVhc9Igoz7ZV~oMeS3C7GU0Owa7OxWrpEIRbzO9bpUQeYpb9NjI-DRkykakXqAWp8~U8YKkUpRM4OA6gGvoJeh0OmlUwutcpTZtrCvhapH51FIyvdC3mT1Qwngqw__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("03", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/7/FnddVAJ7We98txR2TeB6NDAnCEk0.jpg?Expires=1815052132&Signature=AhZacxz462XqgTJDvkD3kfho7A1UHQsntj3Bt1i3s-Z8LGryNdq1UiHN8g8G26c19tmft6m5~FBYNFp7BPiY50GF-RuLmMIeV4XqMx5cZ1fIJhDxvpkq2HPZbe0WYr6~I3cNwTjo-4Zr0cw~MEoflE5L6fyB8YU0XBgJNsEE8HoKzoUbC1KX~OMuGeWwp8xGO9yKVJO0x3kLYCc8J7N0dad7OvzuzP25avC~UAuJ2~HhqnkxPHAkPVl1bM7uvdqx13ikeAYgga0rcQMonUDW4MLzRZoECqHCjWxdxWGpyCFmTw0XZR-pU689w0-qSVTQ6R0q9TofbMdWOmVvy45oNA__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("04", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/7/V5F0ETLU9xfGVPHvLtndO5inPIU0.jpg?Expires=1815043959&Signature=X~vLrWOoLDTKywzcqHVlkoFgsEwfvzzoTJws0guOlc-Vj1WO~RwLJzpRw96uSLlO607Xl9WdHNXGY887ChXC3KfcQAO7DP3jGkn5RRJkWkWZb6Ibf4WEnMyUtONvJQzm0S-BIa4nIUVtRA7lMjLVUzZZE4dk9fWEzLsMiZoDcRGH9bKvPgVsIk0f7Kwb9j9QbfbbgsU7gR01So4zwPpcrivJ7CWuPfjbweU104i1nmfDKz4dM~2o0jFDCjsy6zkOKe1tqMtLI0RCVCMvx2kdpwNlr1QNAZ-wTuGR-~ODoOzi9-arM5ueIk8iZb24SMJtpwZSmTgUoprbO9Y7WS4iHg__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("05", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/7/4djaL0PtXMkXzj24rW3BCZ0nMUQ0.jpg?Expires=1815046105&Signature=1-OdQiy1WE9uhI1IjL5mgZykDGgdMs-cMLivL09vP2si7SNUUfT5azC6V~UpMlp5Ki~g6ihkrEFA~25cQauuMnsbiZGohdTz8JuiOXkmXvKHtDq1VzfRWQ~TsAOj-647z3jTw1e-HaghBZPHdGsfjQhlsmMc3WbjBqRD8AJK2ikTxosWt2w1FH27WBLYUxiG9tF3lRQDkjflj0Pl1LHtg3A4FY4fSwp4WEdjGYlE2dR7Eh51XtP~GDvUMXzOSDmQ1wyy-ItBsg3srAUwGbMdRs3vGciTet00Dma6DWXj~zv~sZypLKWhUTuXj0h8A15L7Wjav05QeqQCiZRFLeZkiA__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("06", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/7/qwqH9QKU0Sc97fIUcYvuYNLpME00.jpg?Expires=1815043273&Signature=E9ydQuoHvzNNx4uO-mw-EPp5gYP6kla~jvpIVyA5CKjqBwtzwO66zU~FhMIsCKNt8qaeBE2IElcLRiPLmHtGZQrwHxHiD0a0MyQ4hJgHOD45dMexx1QrxcLhtOcelNrsMmgGTGmV~isHoOpOfdDf9cH6ydE8dUrqCC248kW6bGXMJdn0mzHoVRb9G-Fudq1xshLgqF8gSpy~ERvUrYz3-7EHNFDWzCS7CJYUo0Vc4drSa5qtHVV-05zKOt1AUY16qYwxuIBuwCvLheWvl96~AoKnKaedUiSxHchVNZYboIniQlISCDEIie74Q-n8RzN78Gy8R~CKOXuIg1bCeCnVFA__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("07", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/7/cHnVjJm1Xj9z0ZwFqWwTZuoUOrc0.jpg?Expires=1815020626&Signature=lFvMYYjzeGrtLmvnHgnNd2VevKAN2iyrc~TwpgsocbHtrdCpl3ah~j6VfE0d1X2VnbaT1iPyNKyPGAxYiG2PvcKuofzqmJqRPXpVSPtRvyPykO~Oz7LfyaQ8sbasTg9pZiRDKrjou7tcNK3EeFajDRWTYZQ-WSuXkaK1vAsXiZKj24jVkVI2aLO6TAAljz0eOiJlQQJhWtVfcUeExNJCN8TwOtlJxFP0LsAdW8ncBoYp0iWb~g3aurE8-is-YJAH6cuBKKaD3blQvM-MTPcJcs5Qiy1tr9FLgdGnTQ5Op-QY369cgn-6qeCq2OjvDPm2dCxQZl3aWhz7d-zjrC39oQ__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("08", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/SxOibbmk8YK8gyrYRuW2PBngcDg0.jpg?Expires=1815003203&Signature=Jn9WgNFPc5c6g8y4r6F5kIqKYi41cdV8iXT-OGK7ngmVuNNX0qFHbRWJcinJQAoI8EuU7K1jhToqNdW8LJQTAlPELjl-0cw45ti4gcZe6pwtFx72gUO5jTokd4J7ETGXpYTLlrKqJIIdJoj20HnuHrAbotAG1CaEuKtgeG~7DU3bxSbuYYfxF8dZ8ppdRNLVzDLe9xZon7PLXbKzI6fyTK2jnsPC6lhUWm3bTXxMtckwV48k6RV6QVpn-L~OdTJx~-6mVvkpcXOcUxjxQ5kYPGvwwP6NOrqxnH2p6o18-17ZonPTh3wZIHg25IXm-VxIfhDVNlUdh~MAqIuJB8IT~w__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("10", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/dNuorUkrJ4kK9BszVg9Yu7h3MGw0.jpg?Expires=1814959886&Signature=am8Z5K8nvUaf0mjVbpI6g~HAxXaOBiH88f30Wddy2MDlEag-7XUssdkCis-c7PYQnPQ3yd8wHpWvcTrKtV7D0Sfn3lg2IssygCDsete4K4HEPxq~KGahXAMPjAciU5nevcfoteKNWdRQYz7IvFsKhNsDbX~~ZJdLSI66rzYArO5WVqSxGjMv1MtD0RPmCOvUkC3QE~61MT9MOMZJfmrQtOQ0UX2uKmHnrgDP~9Zxc2rgQSgpCE-r96zBmsbxWqCQK7rc8QvurVuIYlvVZ44m6jxCC~7kHVzGeHcTud~~9QzZDLVK4d91-NzeZ4itOnfIyqNfD61WV3OnY0uJjCoTGg__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("11", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/e6fZaHZ9U2HzdxX775tW68i4p0U0.jpg?Expires=1814999028&Signature=2yW2x1EtAayaxgzLSDFVWJTEpXIOx4YcbC-~VJRg2K4s2zaDXNOrYqw29M2KAbvHCt1yDz9Tt9Icu~~s0n9W~jV2AbDAgHnXifiNipNQIFkraA50SUS5vHHLh5LGPXFwe6oxfosWKVf26BmIWHHRt1szOHP~jBqjyrupKD4ZMb69uy8hkWdFCLpzVn2pkhnclvcCFLxK1qU0eRRSG7ZYxG2XpkzRNXAJ3nkFDlLRSW15hx72VvHSZVQNEqpib100mi4xVKOpQXm2GGjHq5xuBiG3TjJ7qBfYrQDFTJELl9tKjUiCvl-Mo6YEPUbTluyf9OAf3UaoiTxR6qCs87oxfw__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("12", "pulled_over",     "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/2NeuzZV0LWtDa8000K9ivdDGEPU0.jpg?Expires=1814963493&Signature=EEKI4tnmtdHfMrkr0b2qGy9wEY5G2pS1nJ2w6haTbOxr0HVhWFCRV58DpyxJejgtxGoSP3MGkjPGHweCwfiErLlbIcGq-0f6PdECxsCnn~qK5XqiciiPvcA753XMCSdTeRdZ~bsjAeftuxfs4WNTsL~Hk4LKsBsz1Ma1wrDvuzhiVcEiFIpxSugEOLl~PQ15UXtyffJ2iuvnonIw3vBLYpQX9WHf9kYyRUzLviUDOj-53FbmK-8A76bnNlTTc8GicnShZ7NDGJdWkBsW5RbsFstTB762v3P0cvTTQjYx4IUb8ye-d3hhzobWDItKvDDvLfT5DVpzPAxN3JQ9nUEndg__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("13", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/5Ec0sGKG7lknvHG7iYZswzgHyUk0.jpg?Expires=1814959909&Signature=OssGklFkJaFQzJPuDaXTW25s9hs8NBNz8Ek3Fi7TJcKSO4B4sz12O7NUfjTlN~5NCUolktcCx7rq7WBwAM355oMrDTfDbpzdKzc~vdI8TVGWrq~RhwksBQi0rjKaHE0rnzjDoownsppyKqA8BqbHI3tiXlq8PNxWmkNA-hanS2ktRFjfD42XGN9sAPKrIM-w4YrFPiESlGU4VxPPqjsSn08mSbq2ADX5gil2z-ESJvoG987DzL5MX3Ui2lrLgTVIUYwq3iOiGWM6sV93VDuhiM8alPZ-05fIGXa9OoXdLzdSGE9Q6~5rh54wohd3reT571T7lxIOteYA0KB8Zw5CkQ__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("14", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/I0ibfO0TmLb0C6ivI2XhUuaVOrk0.jpg?Expires=1815000551&Signature=xQf73IZbMxu98OAhbLWBnHXC2npzPx0q1Y-Xn4UcyuRl8tgSD0DNlxYWk-AiQu34SouT~YvhHuxz4TsqJdINJ0KNgWqwSrSR3mEsF6SiVV3yc~zJeVUMxUV0pyBYPu58M~Dg89nt4E2AuaLKDCKYLtXCH72XPZhseRL5m7QeviFKPGBS-VfgNCeYoyLL2XhQPVeKxiw7tgnPPtq1Nw~73uJdXfmw~KmKrywJVvRCvZEIpMg0sTKZHmvLLr6NOEYUJJXOdmna8SpceygVupN0w7S975cbsD8sTdwnFdmClc09GyWwnvESW7Mdw61fPy6wUQTcfyyheQ87KHpnSUgq~w__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("15", "pulled_over",     "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/I0DqgMzNqg8XCm9lmODHfynm0gw0.jpg?Expires=1814990809&Signature=eXZQuFlu0ejsXZHyu-Hdv7rZ93syc-IJ3rykCEdC83rW6nPEbI~j7d6vVH~4c~mp-3fR444ho44jH3TMwdqqjWydf9jL8vcFhy7w1kMYw4RzBOmEN9Hsm5~27sCTwM5zb0AE9sfBPxc4HiP7eMmSbhSrMhiQLJODtMJRJ1B8UpZ8zmhiKLstTCE1NY5iTqkl3ibBeKTi~ZQwGKYMgiM73W9vyf-yga7Domo4XY-1bkWVvz9mqMyFMwZC04SSvbEx91nx74lTq~tw9~w~CaS970iZu8aac~U0ZUMabGCHjEqdXMdS5qTl-ljHAEN0xMGCaiQ7YwW4LKez572nZ~nElA__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("16", "pulled_over",     "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/zIk7Ej60x7g0N2PDvQo14PFeYF40.jpg?Expires=1814996237&Signature=mLMIo4GCyVGlHFG-xoQMdYYb7yaVQQXbtIYXQFjUpv8-qv2C5kZUKO7WSAmkuwk9jzdAos9EMOW6ZqFqLg-I7T-B4cBejRME78BrmfC~8YXpmRKi0xwJFyK-afrN9c3VDuuRLim3RUCKrXqZ2CucNGGt7shuBHTJnr6eLi9ReXJZxUqzFeMdBi9RYPaf~mbM0llYEqdGwm7A7HYA3bEruhfmfp5RhoplLB2cMIefdStW7A8B3BZHXxRM0~cSNGovSm7lYJ18uWFUIGjJrlXra-7pg8mNqOBWTsFBr5E0~It44IUHJbiFu7KCptj0SoBEHyKpJGxxk38VlU7aGqzJtA__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("17", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/7/z4hNDPGai91kUallJNqSYrpySvc0.jpg?Expires=1815009468&Signature=KIPL5Lj5hFvJw4FISsjy06m~SwviNqezWnroLexLss5khXf97rrE8cBCnJ2fIBvT64pG7aXS0T6TA4sOaR7Bt6I1GigtMKa6M303-yvc8nudgBhqe38wk5vxgH2QfyvgfkVeomd~0kgk3MH8ciN7GHbZnZTYJ1tI~a6PlXCvpjuiQS4WAjubLifEAHSNVbz5Js5Eu4w6S2v3VbXZg643flUw3tRayaS-Bj5X83v5Qou6roTSKsvwE0jtsJWCH9W~PLFwu72Ql6SFAFjI89BPz0YmD230e3U3imb34vNgcD6F6IubPFWOkKqajFpRXqUpji-P00WzSPcDavRORwi2Rw__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("18", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/8ofrrY0ZWgz01z00l7q64s2MPgE0.jpg?Expires=1814990332&Signature=DBmU5UndmZfYWG9-I1qeW~h9k55bOZegW9YpHsZTnbqBa5l36F4-jCXG3TyYoP0VAVoO~9b9J2Y6Rr2C4KY0I9Nh5DWqSPpifNw1Pm9XHyP-cjDlfi6vZK~On2SDsxcPBk9Lg8JFXwHiugVmhSshI-4ki8WbFbSs7Q-tjxXazoZN4cbPgy04K7eoGVyThpM-4Gkv77~w4VkP1uBOKcrJgttbi4GfA0yYpzzunF0ILaXMtUDH1iP~10FA1xMrE8IdbtgjfPxzbFJJzi0xDfiDTwhlfXhLQ2FF2pymoMeuNTNfOxvUYXMlzfO34JlN6hlGZIXRORpbIx5y0moIJoejMQ__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("19", "pulled_over",     "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/Dj00jTwTtcApplVCDoUw4TEgUCw0.jpg?Expires=1814991802&Signature=eeuBuR9TzF8V~LnjuSSv1dkY1NMelqiTVvlZCepFQVT~T2uPcSazgBHfUeqrQPmTmceHQi3D1RVd6PIVC4rNn1WkHbIxNdMvs52AkBWtc1FVmFOzpzcvjJzW6c3welEpq4XqiKOjTSKrZTXi7bEjF0CY2ziIShPjAhvdovz7M~pCJFPMk2i4ggek8SbOjALBdDrl3EvIjathcLTrYHXaTt4z-Cn7Ne~aQD6kwRSorFSsNMU5jynoU2i-v0TuiaqyGVXkLJCylFmih6tCbE~vPsjTvUIYbcCK6WGl-SppvOrGevpWjgOAAMVgtbSjqwLR4U2vsyjsQLRG6S5u5UtTfg__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("20", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/SdQ0wWs8XJjT0POF5tesNVKrWow0.jpg?Expires=1814997455&Signature=su54C8Y8N-trbstEDAWEJL32F~7CEzx16lEmR54CdpMo2Z59-bSFlzw2-2JS2mxT3Tl1Xkmx0pBUmOE47hfK3ADQYDw7sFAl5Vnz54t4hcaFS4xo1LAD0FPPr~HjbnK9JP5S8-zJ0jhbSSskisoIC7WrC5DUhZxjYsnltxP4GJinrLRSaenBHaPN-K5uVL-W8UOD4-y9L6JLNj980pSSX23UlD~01Y-ABstZB9RfJeON6LtTUo90xfFUPv0ZLJP8kWIc6yW8FK8J1wDc~Y0WTd~ajieA9SAbSGLO1PJ2v7dLI9rigiWY8au~pRhhfP4~dRyZ~6zlDn2bHYcneTmN5w__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("21", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/V9IGfegBB0kfjk1IZuyuRDqlo0E0.jpg?Expires=1814935262&Signature=l9cZaDtAsmY9iu7lu8NBqomqhvZHRUxpzu3hncl60wIGwy6N9KpgDIolYh9DJjN3yyC5HZczCwJ4vHCsfqm~Yg6PSV5G79-SzIcQEXdNH5uhe4KlLtMqX4B9jDPi5FvkMV2FSxx3USbAYQfiscjRiGgFc11FGdcQ5yWXLKYE0qRBNH7GqrKvNodYDbDWZosgCamtDLt91SU6SBaRo9V4PGcbXgFkdX6x9Of~fq-PC1Ez1KNcjL7gzYNnwLw6JdmghJp7q1pJD1rYZo2omBLDY23J8rxsL6b5W2g-lcDHhkGvA0E~SDkjV5ykGXD~yu1RdEaiBe~6Y7C08hY4yrMZtw__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("22", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/sXAi9mQ0kO5TNFYSdvJ9BkJUQfM0.jpg?Expires=1814993072&Signature=MhjH9Vi~HE86fT9AIhzHf9rRtbQm9CX9RarOrWo4ibRRqCM8G4TsLuaWr5jRgGQGhoxKJ4316mTH7Avs69PtU796l~YV~BGxkz-7CMZQPMd4iTGo58LeoJA1zLkzCJeX1PqVwfJdPa1XEU4ZRHhWFsMkzOKomurnbK85t~73cooJDEbhCro7ns3tC~wAp3PP~KxE9Hab6A1WJHR5s7rB8zeuyiKU5KGWHndHZUfjZuPvQcIIt~3UctiwF26aj4g4YfokW9wN5tuAdVm41~xKhA1Cu5-jfuHtIYHWGWNX3GutrunF3uCTjGvc9F~PksEYZUV3Xk3aFMe8puCPBYxTcw__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("24", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/f9Joun0X4ISg2mWSiLFEK7D5OMM0.jpg?Expires=1814990627&Signature=k~-rj1sgWQTetEYjIA4KeubwNvXY0mGRVSQWvO86v9QnmgxlVxgnJedHnyU3V2v~TidbjEBEMl7PaxF6u7GCRM7apgF9peU9LzkCWoBhjXJ69GMWpaBeENzPX7Y6g7zpxg98FUwRy1YLJb7MATwyoJIixcCRmjK-VA05Wnijai6eeQfRTtiq-U3v0GwGiadcEm8OpaTZLNzi7TN2rJ~qyPrkSnTpiYrDmY~fsiK-GgNFR5T36aQ08VkX6~FGrFVND36S7dpgqj5nslYL2f8beK~y~peGs650gmYVoWGn2m6Hh-W8vZP3J9o0XUsusQ76A2ILM4PDmB03k8bsMptA5w__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("25", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/7F0GtKetx9CNLC2w5abfvWZlEYw0.jpg?Expires=1815004258&Signature=17DPZyF57lQhf8IjAsJUf-cZKbaoxd2hRIs~Yz3P1DZyaPtJQufXhAdWw6JCChJEDVIHy08sPwIgkZk6CNKTsNBpA3vllzuM9VBmgjjG9TJ9dtWnHHvCoW85EzTQvuNEAsK4jdwCYEbfJD~VE0JDIfRZrj1QPd0cRgV275TYyD9P3279-E0B2VvPaUHsmm53xVunObm96syf9zPQxMMYigB41kDNLzgm3fl~fRb0PJHo-40F-c1lNHATtzPy0855gq3E3dw~bb2p95LWELuE~4lahb9ahhRUIgMmvIIvZGj2ax~Ih3GPLT2PBUI96DpuyUzigvwRSUl27cKk4CezWA__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("27", "pulled_over",     "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/5/Tk31QSwKWhXDyHInN3fHjwdfXIE0.jpg?Expires=1814851850&Signature=OvdSCNBc4NKovjN7jdjPRjcq1TWH6nQHOQZg6FteBKObDJKnTrmMe-78VDWQAV0c5WhiqKd-Eo83oLFLyzfmBA0HO0qcsPxYEfCrUq30fUyzK8L9bsHP-oA-il9M7pbbRNuOhD6697qTMLFa7UPfpmV2WDTzqXYIa12BclMaWYV6SajhveceQqBbSOc5z5ZHBNEjkidy1XKInNIKebWG5cxBv1SL2hJYHL~aUZcdQe2wxlD28OYW7OGfhdGtB-YFDfQ8IoFIGxfwHEYLW7ffilBbox3tJaHAj66sqtExINycH4Vt343kU0sfL6NPtxcCbYeqyKxjwTU87WaE9eeZcw__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("30", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/6/M4y0hdyNBMMLtp0h00XrUB46iqM0.jpg?Expires=1814925195&Signature=xXNOE6ALfiXzevAUi~RvV-~r21aIr2HfdaO99SXB33nTzaBjRn~HG3WVUgc0YtcrZJwD65mIO~mMXZf73GcUfJ0bGV37ewMaMCLB2DoNWC9Wq7-G7v4zxgjh6Sx1aYrLFDfKyZOWzxAK6VeIMez6LoamWJX-LoL~cIuK~FtNRLRfJXTcmkCyxiOI-3IGko3iyOsY2IOAeHXJufqkCi~PoqsrGkiWMufSNaoi7q9ktMJuwfyYz8fPfx7ZQDjV4DRm1O43P24GOn2jGiMRUjolRobvlMlpnr~AvFL752l~2PR0eYV2bj0Cf0G3pCgapXqpsGnyv5~L6UDbsfL4InOH9w__&Key-Pair-Id=K1LXZK7POOSHBA"),
    ("32", "unknown_incident", "https://alertmedia.dataminr.com/Prod/vz/image/2026/7/5/UcH0mGh5ZjKHrek4NCijZjkVgHo0.jpg?Expires=1814907062&Signature=vVHjn-Sq86dny68ioWdZcwze2OBoIkpsEBBNAEVDVE7M7UUsUpoHswUmfv0WjVBD7HOgbb6MU24Nz7A5siKVzHc9zSwgYSBf1dQhZz1XqXYrrmNtRLOOHah-aapiIaf1-IjxBBi~rQvZ9fl0S95lEtsneHhWpK-y1Tc3-xaiaANlz5kr8iNAbdE4sW6V45Ue36Y7E7IOfn846Bd-fCpzORyfEkALu00djN0Yf4zowfZxODFt75SHzkfNhwiZMgvb6ZJH5-~cBASAh2gOksl3LSXXqhhoSvRnEpzMttTEKEwe0Oxs3StkW0KD3hvOVhjMesM4PpV16VwsB9kqiQ6qtQ__&Key-Pair-Id=K1LXZK7POOSHBA"),
]


def fetch_image(url, detail="high"):
    with urllib.request.urlopen(url, timeout=30) as resp:
        data = resp.read()
        mime = resp.headers.get_content_type() or "image/jpeg"
    b64 = base64.b64encode(data).decode("utf-8")
    return {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}", "detail": detail}}


def parse_response(text):
    lines = [l.strip() for l in (text or "").strip().splitlines() if l.strip()]
    headline, severity = "", ""
    for line in lines:
        if line in VALID_SEVERITIES:
            severity = line
        elif not headline:
            headline = line
    inc = ""
    h = headline.lower()
    if "crash" in h:               inc = "crash"
    elif "pulled-over" in h:       inc = "pulled_over"
    elif "unknown incident" in h:  inc = "unknown_incident"
    elif "blocked" in h:           inc = "blocked_road"
    elif "construction" in h:      inc = "construction"
    elif "fire" in h:              inc = "fire"
    elif "crowd" in h:             inc = "crowd"
    elif "no incident" in h:       inc = "no_incident_visible"
    return headline, severity, inc


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="openai/gpt-4o-mini")
    ap.add_argument("--detail", default="high", choices=["high", "low", "auto"])
    args = ap.parse_args()

    api_key = os.environ.get("ANTHROPIC_AUTH_TOKEN") or os.environ.get("LITELLM_API_KEY")
    if not api_key:
        sys.exit("ERROR: set ANTHROPIC_AUTH_TOKEN env var")

    client = openai.OpenAI(base_url=LITELLM_PROXY_URL, api_key=api_key)

    print(f"Model: {args.model}  |  Detail: {args.detail}")
    print(f"Testing {len(IMAGES)} false-positive images (previously predicted crash)")
    print(f"{'#':<4}  {'GT':<18}  {'Predicted':<18}  {'Result':<8}  Headline")
    print("-" * 90)

    fixed = still_crash = other = errors = 0

    for label, gt, url in IMAGES:
        try:
            img = fetch_image(url, args.detail)
            t0 = time.monotonic()
            resp = client.chat.completions.create(
                model=args.model,
                messages=[{"role": "user", "content": [img, {"type": "text", "text": HEADLINE_PROMPT}]}],
                max_tokens=120,
                timeout=90,
            )
            ms = round((time.monotonic() - t0) * 1000)
            headline, severity, inc = parse_response(resp.choices[0].message.content)

            if inc == "crash":
                result = "STILL FP"
                still_crash += 1
            elif inc in (gt, "unknown_incident"):
                result = "FIXED ✓"
                fixed += 1
            else:
                result = f"→ {inc}"
                other += 1

            print(f"{label:<4}  {gt:<18}  {inc:<18}  {result:<8}  {headline[:45]}  ({ms}ms)")
        except Exception as e:
            print(f"{label:<4}  {gt:<18}  {'ERROR':<18}  {'ERROR':<8}  {e}")
            errors += 1

    total = len(IMAGES) - errors
    print("-" * 90)
    print(f"Fixed: {fixed}/{total} ({fixed/total*100:.0f}%)  |  Still crash: {still_crash}  |  Other: {other}  |  Errors: {errors}")


if __name__ == "__main__":
    main()
