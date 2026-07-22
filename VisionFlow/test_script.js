
const presetPersonas = {
  indoor: {
    age: 35,
    gender: 'male',
    ethnicity: 'Caucasian American',
    location: 'New York, USA',
    language: 'English',
    accent: 'Standard American',
    occupation: 'Master Interior Contractor & Designer',
    experience_years: 10,
    personal_tags: 'High-end interior remodel, modern minimalist, smart home integration, transparent pricing',
    target_audience: 'New homeowners, couples upgrading their living space, luxury condo owners',
    personality: 'Detail-oriented, practical, trustworthy, passionate about craftsmanship',
    content_style: 'Process breakdowns, before-and-after reveals, DIY tips, tool reviews',
    portrait_description: '35-year-old Caucasian male, rugged but neat, wearing a tool belt over a branded polo shirt, confident and approachable'
  },
  outdoor: {
    age: 42,
    gender: 'male',
    ethnicity: 'Hispanic American',
    location: 'Texas, USA',
    language: 'English',
    accent: 'Slight Texas drawl',
    occupation: 'Professional Landscape Architect & Hardscape Expert',
    experience_years: 15,
    personal_tags: 'Backyard oasis, curb appeal, patio building, drought-resistant landscaping',
    target_audience: 'Suburban homeowners, families wanting outdoor entertainment areas',
    personality: 'Energetic, outdoor-loving, creative problem solver, down-to-earth',
    content_style: 'Time-lapse builds, plant selection guides, budget patio makeovers, outdoor living tips',
    portrait_description: '42-year-old Hispanic male, sun-tanned, wearing a wide-brim hat and sunglasses, holding blueprints in a sunny backyard'
  },
  cabinet: {
    age: 30,
    gender: 'female',
    ethnicity: 'Asian American',
    location: 'Seattle, USA',
    language: 'English',
    accent: 'Standard American',
    occupation: 'Custom Cabinetry Designer & Organization Expert',
    experience_years: 6,
    personal_tags: 'Kitchen remodeling, space optimization, custom storage, luxury hardware',
    target_audience: 'Homeowners doing kitchen/bath remodels, people looking for organization hacks',
    personality: 'Elegant, highly organized, aesthetic-driven, helpful and precise',
    content_style: 'Close-ups of cabinet features, soft-close hinges, clever storage hacks, sleek kitchen tours',
    portrait_description: '30-year-old Asian American woman, chic business casual attire, standing in a sleek modern kitchen, warm professional smile'
  },
  furniture: {
    age: 28,
    gender: 'female',
    ethnicity: 'Second-generation Indian American',
    location: 'California, USA',
    language: 'English',
    accent: 'slight Indian accent',
    occupation: 'Full-time high-end real estate agent & Furniture Stylist',
    experience_years: 4,
    personal_tags: 'Expert in US real estate, home renovation specialist, high ROI remodeling, American light luxury / Indian fusion decor',
    target_audience: 'US homebuyers aged 25-45, new homeowners, old house renovators, budget-friendly remodeling seekers',
    personality: 'Enthusiastic, straightforward, loves sharing, loves showing off clients\' home transformations',
    content_style: 'Blends Indian aesthetics with American decor, shares highly cost-effective renovation plans and mistake-avoidance guides',
    portrait_description: '28-year-old Indian American woman, confident and approachable, professional yet stylish real estate agent attire, warm smile'
  }
};

function fillPreset(type) {
  const data = presetPersonas[type];
  if (!data) return;
  document.getElementById('p-age').value = data.age;
  document.getElementById('p-gender').value = data.gender;
  document.getElementById('p-ethnicity').value = data.ethnicity;
  document.getElementById('p-location').value = data.location;
  document.getElementById('p-language').value = data.language;
  document.getElementById('p-accent').value = data.accent;
  document.getElementById('p-occupation').value = data.occupation;
  document.getElementById('p-experience').value = data.experience_years;
  document.getElementById('p-tags').value = data.personal_tags;
  document.getElementById('p-audience').value = data.target_audience;
  document.getElementById('p-personality').value = data.personality;
  document.getElementById('p-content-style').value = data.content_style;
  document.getElementById('p-portrait').value = data.portrait_description;
}

function submitPlan() {
  const persona = {
    age: parseInt(document.getElementById('p-age').value) || 28,
    gender: document.getElementById('p-gender').value,
    ethnicity: document.getElementById('p-ethnicity').value,
    location: document.getElementById('p-location').value,
    language: document.getElementById('p-language').value,
    accent: document.getElementById('p-accent').value,
    occupation: document.getElementById('p-occupation').value,
    experience_years: parseInt(document.getElementById('p-experience').value) || 0,
    personal_tags: document.getElementById('p-tags').value,
    target_audience: document.getElementById('p-audience').value,
    personality: document.getElementById('p-personality').value,
    content_style: document.getElementById('p-content-style').value,
    portrait_description: document.getElementById('p-portrait').value,
    extra_info: document.getElementById('p-extra').value,
  };
  const genVideos = document.getElementById('p-gen-videos').checked;

  fetch('/api/plans', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({persona, generate_videos: genVideos}),
  })
  .then(r => r.json())
  .then(data => {
    if (data.job_id) {
      // Poll until done, then redirect
      const pollJob = () => {
        fetch('/api/jobs/' + data.job_id)
          .then(r => r.json())
          .then(job => {
            if (job.status === 'running') {
              setTimeout(pollJob, 3000);
            } else if (job.status === 'done' && job.plan_id) {
              window.location.href = '/plan/' + job.plan_id;
            } else {
              alert('规划失败: ' + (job.error || '未知错误'));
            }
          });
      };
      pollJob();
    } else {
      alert('启动失败: ' + (data.error || JSON.stringify(data)));
    }
  });
}
