# Apple Health Shortcut Setup

Your coach can pull health data from your Apple Watch automatically. This takes about 5 minutes to set up. Once it's running, your health data syncs every morning without you touching anything.

## What it does

A small automation on your iPhone reads your health data from Apple Health and sends a daily snapshot to your coach. It grabs: resting heart rate, HRV, steps, sleep, weight, VO2 max, blood oxygen, active calories, and respiratory rate. Whatever your Apple Watch tracks, it sends.

## Step 1: Create the Shortcut

Open the **Shortcuts** app on your iPhone and tap the **+** button to create a new shortcut. Name it **"Sync Health Data"**.

Add the following actions in order. For each one, tap **"Add Action"** and search for the action name.

### 1a. Set up your credentials

Add a **Text** action. Paste this:
```
YOUR_TOKEN_HERE
```
Tap the result and rename it to `Token`.

Add another **Text** action. Type your user ID (your coach will tell you this, e.g. `paul`). Rename it to `UserID`.

### 1b. Get Resting Heart Rate

- Add **Find Health Samples**
- Set Type to **Resting Heart Rate**
- Set Start Date to **"is in the last 1 day"**
- Sort by **Start Date**, **Most Recent First**
- Limit to **1**
- Tap the output and rename it to `RHR`

Add a **Get Details of Health Sample** action, choose **Value** from `RHR`. Rename the result to `RHR_Value`.

### 1c. Get Heart Rate Variability

- Add **Find Health Samples**
- Set Type to **Heart Rate Variability**
- Set Start Date to **"is in the last 1 day"**
- Sort by **Start Date**, **Most Recent First**
- Limit to **1**

Add **Get Details of Health Sample** > **Value**. Rename to `HRV_Value`.

### 1d. Get Steps

- Add **Find Health Samples**
- Set Type to **Step Count**
- Set Start Date to **"is in the last 1 day"**
- Group By: **Day**

Add **Get Details of Health Sample** > **Value**. Rename to `Steps_Value`.

### 1e. Get Sleep

- Add **Find Health Samples**
- Set Type to **Sleep Analysis**
- Set Start Date to **"is in the last 1 day"**
- Sort by **Start Date**, **Oldest First**

This returns your sleep samples. To get total hours:

- Add **Count** (counts the samples)
- Add **Get Details of Health Sample** > **Duration** from the sleep samples
- Add **Calculate Statistics** > **Sum**
- Add **Calculate** > divide the sum by **3600** (converts seconds to hours)
- Rename the result to `Sleep_Hours`

For sleep start time:
- From the sleep samples, add **Get Details of Health Sample** > **Start Date**
- Get the **first item** from the list
- Add **Format Date** > Custom format: **HH:mm**
- Rename to `Sleep_Start`

For sleep end time:
- From the sleep samples, add **Get Details of Health Sample** > **End Date**
- Get the **last item** from the list
- Add **Format Date** > Custom format: **HH:mm**
- Rename to `Sleep_End`

### 1f. Get Weight

- Add **Find Health Samples**
- Set Type to **Weight**
- Sort by **Start Date**, **Most Recent First**
- Limit to **1**

Add **Get Details of Health Sample** > **Value**. Rename to `Weight_Value`.

Note: Make sure the value is in **pounds**. If your Health app uses kg, add a **Calculate** action to multiply by 2.205.

### 1g. Get VO2 Max

- Add **Find Health Samples**
- Set Type to **Cardio Fitness** (this is VO2 Max)
- Sort by **Start Date**, **Most Recent First**
- Limit to **1**

Add **Get Details of Health Sample** > **Value**. Rename to `VO2_Value`.

### 1h. Get Blood Oxygen

- Add **Find Health Samples**
- Set Type to **Blood Oxygen**
- Set Start Date to **"is in the last 1 day"**
- Sort by **Start Date**, **Most Recent First**
- Limit to **1**

Add **Get Details of Health Sample** > **Value**. Rename to `SpO2_Value`.

### 1i. Get Active Calories

- Add **Find Health Samples**
- Set Type to **Active Energy Burned**
- Set Start Date to **"is in the last 1 day"**
- Group By: **Day**

Add **Get Details of Health Sample** > **Value**. Rename to `ActiveCal_Value`.

### 1j. Get Respiratory Rate

- Add **Find Health Samples**
- Set Type to **Respiratory Rate**
- Set Start Date to **"is in the last 1 day"**
- Sort by **Start Date**, **Most Recent First**
- Limit to **1**

Add **Get Details of Health Sample** > **Value**. Rename to `RespRate_Value`.

### 1k. Build the JSON and send it

Add a **Dictionary** action with these key/value pairs:

| Key | Value |
|-----|-------|
| `token` | `Token` variable |
| `user_id` | `UserID` variable |
| `timestamp` | Current Date (ISO 8601) |

Now add a nested dictionary for `metrics`:

| Key | Value |
|-----|-------|
| `resting_hr` | `RHR_Value` |
| `hrv_sdnn` | `HRV_Value` |
| `steps` | `Steps_Value` |
| `sleep_hours` | `Sleep_Hours` |
| `sleep_start` | `Sleep_Start` |
| `sleep_end` | `Sleep_End` |
| `weight_lbs` | `Weight_Value` |
| `vo2_max` | `VO2_Value` |
| `blood_oxygen` | `SpO2_Value` |
| `active_calories` | `ActiveCal_Value` |
| `respiratory_rate` | `RespRate_Value` |

**Tip:** In Shortcuts, you can build a nested dictionary by adding a Dictionary action for `metrics` first, then putting that dictionary as the value of the `metrics` key in the outer dictionary. Alternatively, use a **Text** action to write the full JSON manually:

```json
{
  "token": "[Token]",
  "user_id": "[UserID]",
  "timestamp": "[Current Date]",
  "metrics": {
    "resting_hr": [RHR_Value],
    "hrv_sdnn": [HRV_Value],
    "steps": [Steps_Value],
    "sleep_hours": [Sleep_Hours],
    "sleep_start": "[Sleep_Start]",
    "sleep_end": "[Sleep_End]",
    "weight_lbs": [Weight_Value],
    "vo2_max": [VO2_Value],
    "blood_oxygen": [SpO2_Value],
    "active_calories": [ActiveCal_Value],
    "respiratory_rate": [RespRate_Value]
  }
}
```

Then add **Get Contents of URL**:
- URL: `http://YOUR_GATEWAY_IP:18800/api/ingest_health_snapshot`
- Method: **POST**
- Request Body: **JSON** (if using Dictionary) or **File** (if using Text action for raw JSON)
- Headers: `Content-Type` = `application/json`

Add a final **Show Notification** action:
- Title: "Health Sync"
- Body: "Daily health data sent to coach"

## Step 2: Set up the daily automation

1. Open the **Shortcuts** app
2. Tap the **Automation** tab at the bottom
3. Tap **+** then **Create Personal Automation**
4. Choose **Time of Day**
5. Set to **7:00 AM** (or whenever you wake up, the data covers the last 24 hours)
6. Choose **Daily**
7. Tap **Next**
8. Add a **Run Shortcut** action and select **"Sync Health Data"**
9. Tap **Next**
10. **Turn off "Ask Before Running"** (this is the key step for hands-free operation)
11. Tap **Done**

On iOS 18.4+, the "Ask Before Running" toggle is under the automation's settings. On older iOS versions, it appears as a toggle after you set up the automation.

## First run

The very first time the shortcut runs, Apple will ask permission to read each health data type. Tap **Allow** for each one. After that first run, it's fully automatic.

You'll see a small notification banner when the automation fires each morning. That's normal and expected.

## Troubleshooting

**"Couldn't communicate with a helper application"**: This usually means the Health permission prompt was dismissed. Open the Shortcuts app, run the shortcut manually, and accept the permissions.

**Missing metrics**: If a metric shows as `null` or 0, it means your Apple Watch didn't record that data type recently. Steps and sleep should always be present. VO2 Max only updates after outdoor walks or runs with GPS. Blood oxygen requires overnight monitoring to be enabled (Settings > Health > Blood Oxygen > check "During Sleep" or similar).

**Network error**: Make sure your phone can reach the gateway IP. If you're on the same WiFi as the Mac Mini, use the local IP. If you're on cellular, you'll need the tunnel domain instead.

**Wrong weight unit**: Apple Health may report weight in kg depending on your locale. Add a calculation step to convert: multiply by 2.205 to get pounds.

## How it works (for the curious)

The shortcut reads 9 metrics from Apple Health's HealthKit database on your phone. It packages them as a JSON object and POSTs them to the Kiso gateway. The gateway:

1. Validates your token
2. Appends the snapshot to a daily time series (`apple_health_daily.json`)
3. Updates rolling 7-day averages in `apple_health_latest.json` (same format the scoring engine reads)
4. If weight is included, also logs it to your weight tracker
5. Returns a summary of what was stored

The rolling average file is what your coach uses for scoring and insights. After a week of daily syncs, the averages stabilize and your health picture becomes reliable.

**A note on HRV**: Apple Watch measures HRV as SDNN (standard deviation of normal-to-normal intervals). Some other devices use RMSSD. Both are valid measures of heart rate variability, but the numbers aren't directly comparable. Your coach knows this and will interpret accordingly.
