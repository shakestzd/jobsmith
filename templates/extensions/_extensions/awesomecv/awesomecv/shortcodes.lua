-- Simple YAML parser for our specific structure
function parse_resume_yaml(content)
    local items = {}
    local current_item = nil

    for line in content:gmatch("[^\r\n]+") do
        line = line:gsub("^%s+", ""):gsub("%s+$", "") -- trim

        if line:match("^- title:") then
            if current_item then
                table.insert(items, current_item)
            end
            current_item = {
                title = line:match('^- title: "([^"]+)"') or line:match("^- title: (.+)"),
                location = "",
                date = "",
                description = "",
                details = {}
            }
        elseif current_item and line:match("^location:") then
            current_item.location = line:match('^location: "([^"]+)"') or line:match("^location: (.+)")
        elseif current_item and line:match("^date:") then
            current_item.date = line:match('^date: "([^"]+)"') or line:match("^date: (.+)")
        elseif current_item and line:match("^description:") then
            current_item.description = line:match('^description: "([^"]+)"') or line:match("^description: (.+)")
        elseif current_item and line:match('^- "') then
            local detail = line:match('^- "([^"]+)"')
            if detail then
                table.insert(current_item.details, detail)
            end
        end
    end

    if current_item then
        table.insert(items, current_item)
    end

    return items
end

function yaml(args, kwargs, meta)
    local filepath = args[1]

    -- For Typst format, check if this is the skills file and use appropriate function
    if FORMAT == "typst" then
        if filepath:match("skill%.yml") then
            -- Get skills configuration from metadata (passed as parameter)
            local skills_format = "grid"
            local emphasis_dict = "()"

            if meta and meta["skills-format"] then
                skills_format = pandoc.utils.stringify(meta["skills-format"])
            end

            if meta and meta["skills-emphasis"] then
                local skills_emphasis = meta["skills-emphasis"]
                emphasis_dict = "("

                for key, value in pairs(skills_emphasis) do
                    if type(value) == "table" then
                        local items = "("
                        -- Handle both MetaList and regular table structures
                        if value.t == "MetaList" then
                            for _, item in ipairs(value) do
                                items = items .. '"' .. pandoc.utils.stringify(item) .. '", '
                            end
                        else
                            for _, item in ipairs(value) do
                                items = items .. '"' .. pandoc.utils.stringify(item) .. '", '
                            end
                        end
                        items = items:gsub(", $", "") .. ")"
                        emphasis_dict = emphasis_dict .. '"' .. key .. '": ' .. items .. ', '
                    end
                end
                emphasis_dict = emphasis_dict:gsub(", $", "") .. ")"
            end

            return pandoc.RawInline('typst', '#data-to-skill-items(data: yaml("' .. filepath .. '"), emphasis-config: ' .. emphasis_dict .. ', format-type: "' .. skills_format .. '")')
        else
            return pandoc.RawInline('typst', '#data-to-resume-entries(data: yaml("' .. filepath .. '"))')
        end
    else
        -- For HTML and other formats, parse and render the YAML
        local file = io.open(filepath, "r")
        if not file then
            return pandoc.Para({pandoc.Emph({pandoc.Str("Could not read file: " .. filepath)})})
        end

        local content = file:read("*all")
        file:close()

        local items = parse_resume_yaml(content)
        local blocks = {}

        for _, item in ipairs(items) do
            -- Create title line: **Title** | Description | Location
            local title_parts = {pandoc.Strong({pandoc.Str(item.title)})}
            if item.description and item.description ~= "" then
                table.insert(title_parts, pandoc.Str(" | " .. item.description))
            end
            if item.location and item.location ~= "" then
                table.insert(title_parts, pandoc.Str(" | " .. item.location))
            end

            table.insert(blocks, pandoc.Para(title_parts))

            -- Add date in italics
            if item.date and item.date ~= "" then
                table.insert(blocks, pandoc.Para({pandoc.Emph({pandoc.Str(item.date)})}))
            end

            -- Add details as bullet list
            if #item.details > 0 then
                local list_items = {}
                for _, detail in ipairs(item.details) do
                    table.insert(list_items, {pandoc.Para({pandoc.Str(detail)})})
                end
                table.insert(blocks, pandoc.BulletList(list_items))
            end

            -- Add spacing between items
            table.insert(blocks, pandoc.Para({pandoc.Str("")}))
        end

        return blocks
    end
end
