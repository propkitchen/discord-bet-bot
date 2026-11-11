/**
 * Illustrative wagers ONLY. No historical PropKitchen stats, Discord user
 * data, production IDs, or database access is included in this preview.
 */
export type WagerResult = "Win" | "Loss" | "Push" | "Void" | "Pending";
export type WagerCategory = "Straight" | "DFS Slip" | "Sportsbook Parlay";

export type DemoWager = {
  id: string;
  date: string; // ISO local betting date, YYYY-MM-DD
  capper: string;
  sport: string;
  player: string;
  team: string;
  selection: string;
  market: string;
  category: WagerCategory;
  platform: string;
  odds: string;
  risk: number;
  result: WagerResult;
  net: number; // positive profit or negative loss, in units
};

export const demoWagers: DemoWager[] = [
  { id:"D01",date:"2026-10-03",capper:"Sample Chef",sport:"NFL",player:"Quarterback A",team:"Team Red",selection:"Quarterback A o225.5 Pass Yds",market:"Passing Yards",category:"Straight",platform:"FanDuel",odds:"-115",risk:1,result:"Win",net:0.87 },
  { id:"D02",date:"2026-10-04",capper:"Sample Chef",sport:"NBA",player:"Forward B",team:"Team Blue",selection:"Forward B o20.5 Pts",market:"Points",category:"Straight",platform:"DraftKings",odds:"-110",risk:1,result:"Loss",net:-1 },
  { id:"D03",date:"2026-10-05",capper:"Sample Chef",sport:"MLB",player:"Pitcher C",team:"Team Gold",selection:"Pitcher C o5.5 Ks",market:"Strikeouts",category:"Straight",platform:"BetMGM",odds:"+105",risk:0.75,result:"Win",net:0.79 },
  { id:"D04",date:"2026-10-06",capper:"Sample Chef",sport:"NBA",player:"Multiple Players",team:"Mixed",selection:"4-pick flex slip",market:"Multi-leg",category:"DFS Slip",platform:"PrizePicks",odds:"2.1x",risk:0.5,result:"Win",net:0.55 },
  { id:"D05",date:"2026-10-07",capper:"Sample Chef",sport:"NFL",player:"Receiver D",team:"Team Red",selection:"Receiver D o4.5 Rec",market:"Receptions",category:"Straight",platform:"FanDuel",odds:"-125",risk:1,result:"Loss",net:-1 },
  { id:"D06",date:"2026-10-09",capper:"Sample Chef",sport:"Soccer",player:"Striker E",team:"City FC",selection:"Striker E anytime goal",market:"Goals",category:"Straight",platform:"Fanatics",odds:"+120",risk:0.5,result:"Win",net:0.6 },
  { id:"D07",date:"2026-10-11",capper:"Sample Chef",sport:"NBA",player:"Multiple Players",team:"Mixed",selection:"2-leg NBA double-double lotto",market:"Multi-leg",category:"Sportsbook Parlay",platform:"DraftKings",odds:"+450",risk:0.2,result:"Loss",net:-0.2 },
  { id:"D08",date:"2026-10-12",capper:"Sample Chef",sport:"NFL",player:"Running Back F",team:"Team Green",selection:"Running Back F o64.5 Rush Yds",market:"Rushing Yards",category:"Straight",platform:"Hard Rock",odds:"-110",risk:1,result:"Win",net:0.91 },
  { id:"D09",date:"2026-10-14",capper:"Sample Chef",sport:"NBA",player:"Guard G",team:"Team Blue",selection:"Guard G o7.5 Ast",market:"Assists",category:"Straight",platform:"Bet365",odds:"-130",risk:1,result:"Push",net:0 },
  { id:"D10",date:"2026-10-15",capper:"Sample Chef",sport:"Tennis",player:"Player H",team:"Tour",selection:"Player H match winner",market:"Moneyline",category:"Straight",platform:"BetMGM",odds:"-105",risk:0.5,result:"Win",net:0.48 },
  { id:"D11",date:"2026-10-16",capper:"Sample Chef",sport:"NBA",player:"Multiple Players",team:"Mixed",selection:"5-pick power slip",market:"Multi-leg",category:"DFS Slip",platform:"Chalkboard",odds:"4x",risk:0.3,result:"Loss",net:-0.3 },
  { id:"D12",date:"2026-10-18",capper:"Sample Chef",sport:"NFL",player:"Quarterback A",team:"Team Red",selection:"Quarterback A o1.5 Pass TDs",market:"Passing TDs",category:"Straight",platform:"FanDuel",odds:"+110",risk:1,result:"Win",net:1.1 },
  { id:"D13",date:"2026-10-19",capper:"Sample Chef",sport:"MLB",player:"Hitter J",team:"Team Gold",selection:"Hitter J o1.5 TB",market:"Total Bases",category:"Straight",platform:"DraftKings",odds:"-120",risk:0.5,result:"Loss",net:-0.5 },
  { id:"D14",date:"2026-10-20",capper:"Sample Chef",sport:"NBA",player:"Center K",team:"Team Blue",selection:"Center K o12.5 Reb",market:"Rebounds",category:"Straight",platform:"Bet365",odds:"-115",risk:1,result:"Win",net:0.87 },
  { id:"D15",date:"2026-10-21",capper:"Sample Chef",sport:"NBA",player:"Forward B",team:"Team Blue",selection:"Forward B o25.5 Pts+Reb",market:"Points+Rebounds",category:"Straight",platform:"FanDuel",odds:"-110",risk:1,result:"Win",net:0.91 },
  { id:"D16",date:"2026-10-22",capper:"Sample Chef",sport:"NFL",player:"Multiple Players",team:"Mixed",selection:"3-leg same-game parlay",market:"Multi-leg",category:"Sportsbook Parlay",platform:"FanDuel",odds:"+310",risk:0.3,result:"Win",net:0.93 },
  { id:"D17",date:"2026-10-23",capper:"Sample Chef",sport:"NBA",player:"Guard G",team:"Team Blue",selection:"Guard G o2.5 Threes",market:"Three-Pointers",category:"Straight",platform:"DraftKings",odds:"+125",risk:0.5,result:"Loss",net:-0.5 },
  { id:"D18",date:"2026-10-24",capper:"Sample Chef",sport:"NFL",player:"Running Back F",team:"Team Green",selection:"Running Back F o79.5 Rush+Rec Yds",market:"Rush+Rec Yards",category:"Straight",platform:"BetMGM",odds:"-115",risk:1,result:"Void",net:0 },
  { id:"D19",date:"2026-10-25",capper:"Sample Chef",sport:"NBA",player:"Multiple Players",team:"Mixed",selection:"6-pick flex slip",market:"Multi-leg",category:"DFS Slip",platform:"Underdog",odds:"2x",risk:0.3,result:"Win",net:0.3 },
  { id:"D20",date:"2026-10-26",capper:"Sample Chef",sport:"Soccer",player:"Striker E",team:"City FC",selection:"Striker E o3.5 Shots",market:"Shots",category:"Straight",platform:"FanDuel",odds:"-125",risk:1,result:"Loss",net:-1 },
  { id:"D21",date:"2026-10-27",capper:"Sample Chef",sport:"NBA",player:"Guard G",team:"Team Blue",selection:"Guard G o6.5 Ast",market:"Assists",category:"Straight",platform:"DraftKings",odds:"-115",risk:1,result:"Win",net:0.87 },
  { id:"D22",date:"2026-10-28",capper:"Sample Chef",sport:"NBA",player:"Center K",team:"Team Blue",selection:"Center K o11.5 Reb",market:"Rebounds",category:"Straight",platform:"Bet365",odds:"-120",risk:0.75,result:"Pending",net:0 },
  { id:"D23",date:"2026-10-29",capper:"Sample Chef",sport:"NFL",player:"Receiver D",team:"Team Red",selection:"Receiver D o68.5 Rec Yds",market:"Receiving Yards",category:"Straight",platform:"Hard Rock",odds:"-115",risk:1,result:"Pending",net:0 },
  { id:"D24",date:"2026-10-30",capper:"Sample Chef",sport:"NBA",player:"Multiple Players",team:"Mixed",selection:"3-pick power slip",market:"Multi-leg",category:"DFS Slip",platform:"Chalkboard",odds:"3x",risk:0.4,result:"Pending",net:0 },
  { id:"D25",date:"2026-10-30",capper:"Demo Analyst",sport:"NBA",player:"Guard L",team:"Team Silver",selection:"Guard L o18.5 Pts",market:"Points",category:"Straight",platform:"FanDuel",odds:"-110",risk:1,result:"Win",net:0.91 },
  { id:"D26",date:"2026-10-29",capper:"Demo Analyst",sport:"NFL",player:"Quarterback M",team:"Team Orange",selection:"Quarterback M o240.5 Pass Yds",market:"Passing Yards",category:"Straight",platform:"DraftKings",odds:"-125",risk:1,result:"Loss",net:-1 },
  { id:"D27",date:"2026-10-28",capper:"Demo Analyst",sport:"Soccer",player:"Striker N",team:"United FC",selection:"Striker N anytime goal",market:"Goals",category:"Straight",platform:"Bet365",odds:"+160",risk:0.5,result:"Win",net:0.8 },
  { id:"D28",date:"2026-10-27",capper:"Demo Analyst",sport:"NBA",player:"Multiple Players",team:"Mixed",selection:"4-pick flex slip",market:"Multi-leg",category:"DFS Slip",platform:"PrizePicks",odds:"2x",risk:0.4,result:"Loss",net:-0.4 }
];
